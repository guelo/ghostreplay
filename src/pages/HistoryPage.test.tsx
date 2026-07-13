import { describe, it, expect, vi, beforeEach, afterEach, beforeAll } from 'vitest';
import { forwardRef, useImperativeHandle } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import HistoryPage from './HistoryPage';
import { ApiError, type AnalysisMove } from '../utils/api';

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
// useImperativeHandle so the HistoryPage ref's jumpToMove is observable. The
// `moves` prop is rendered as data attributes (read back by `boardMoves()`) so a
// projection test cannot pass on the stats pane while the board is still handed
// unprojected moves.
const mockJumpToMove = vi.fn();
vi.mock('../components/AnalysisBoard', () => ({
  default: forwardRef(
    (
      {
        boardOrientation,
        initialMoveIndex,
        footer,
        mobileToolbar,
        sessionId,
        moves,
      }: {
        boardOrientation: string;
        initialMoveIndex?: number;
        footer?: React.ReactNode;
        mobileToolbar?: React.ReactNode;
        sessionId?: string;
        moves: AnalysisMove[];
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
          {moves?.map((move, index) => (
            <span
              key={index}
              data-testid="board-move"
              data-classification={move.classification}
              data-delta={String(move.eval_delta)}
            />
          ))}
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

/** Matches projectExactBest's own default — the fen_before of ply 0. */
const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

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
  // No total_moves: /session/{id}/analysis does not send one (the move list carries it).
  summary: { blunders: 0, mistakes: 0, inaccuracies: 0, average_centipawn_loss: 0, accuracy: 88 },
};

/** An ended game that was never analyzed: no player move has an eval_delta. */
const UNANALYZED_HISTORY_RESPONSE = [
  {
    session_id: 'abc-123',
    player_color: 'white',
    result: 'checkmate_win',
    engine_elo: 1500,
    ended_at: '2026-04-20T12:00:00Z',
    opening_name: 'Sicilian Defense',
    summary: {
      total_moves: 0,
      blunders: 0,
      mistakes: 0,
      inaccuracies: 0,
      average_centipawn_loss: null,
      accuracy: null,
    },
  },
];

/** The value cell sitting next to a given label in the no-analysis summary panel. */
const summaryStat = (label: string): Element => {
  const value = screen.getByText(label).previousElementSibling;
  if (!value) throw new Error(`no value cell for ${label}`);
  return value;
};

describe('HistoryPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setMatchMedia(false);
    mockFetchSessionOpenings.mockResolvedValue({ player_color: 'white', lineage: [], start_ply: 1 });
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
    // Passes the selected game's session id to the evidence driver.
    expect(screen.getByTestId('analysis-board')).toHaveAttribute('data-session-id', 'abc-123');
  });

  it('cycles the board through the player union when the You header is clicked', async () => {
    const user = userEvent.setup();
    mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
    // playerColor = white → player moves at even indices: blunder@0, mistake@2,
    // inaccuracy@4. Union sorted by index = [0, 2, 4].
    mockFetchAnalysis.mockResolvedValue({
      ...ANALYSIS_RESPONSE,
      moves: [
        { move_san: 'e4', fen_after: 'fen0', color: 'white', classification: 'blunder' },
        { move_san: 'c5', fen_after: 'fen1', color: 'black', classification: 'mistake' },
        { move_san: 'Nf3', fen_after: 'fen2', color: 'white', classification: 'mistake' },
        { move_san: 'd6', fen_after: 'fen3', color: 'black', classification: null },
        { move_san: 'd4', fen_after: 'fen4', color: 'white', classification: 'inaccuracy' },
      ],
    });

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    await screen.findByTestId('analysis-board');

    const you = screen.getByRole('button', {
      name: /all of your blunders, mistakes, and inaccuracies/i,
    });
    await user.click(you);
    expect(mockJumpToMove).toHaveBeenLastCalledWith(0);
    await user.click(you);
    expect(mockJumpToMove).toHaveBeenLastCalledWith(2);
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
          moves: ['e4'],
        },
      ],
      start_ply: 1,
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
          // moves is the played SAN prefix up to and including the crossing move
          // (3 moves), so the jump targets index moves.length - 1 = 2 (guards
          // against a hardcoded-zero jump).
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
          moves: ['e4', 'c5', 'Nf3'],
        },
      ],
      start_ply: 1,
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

  it('does not jump the board when the crossing move is beyond the loaded analysis', async () => {
    const user = userEvent.setup();
    mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
    // Analysis has only 3 moves loaded...
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);
    mockFetchSessionOpenings.mockResolvedValue({
      player_color: 'white',
      lineage: [
        {
          opening_key: 'deep-key',
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
          // ...but this opening's crossing move is index 4 (5-move prefix), out
          // of range for the 3 loaded analysis moves, so the guard skips the jump.
          moves: ['e4', 'c5', 'Nf3', 'Nc6', 'Bb5'],
        },
      ],
      start_ply: 1,
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
          moves: ['e4'],
        },
      ],
      start_ply: 1,
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

  // The no-analysis fallback panel is the only place the /api/history
  // average_centipawn_loss field is rendered — and the surface where an unanalyzed
  // game used to read as "0", i.e. perfect play.
  //
  // It is reached by a TERMINAL analysis failure (g-22t8.3): a transient rejection now
  // keeps the pane in its loading state while the retries run, so the fallback can no
  // longer flicker in mid-poll as though it were the final answer.
  describe('no-analysis summary panel', () => {
    it('renders an em-dash for Avg CPL when no player move was evaluated', async () => {
      mockFetchHistory.mockResolvedValue(UNANALYZED_HISTORY_RESPONSE);
      mockFetchAnalysis.mockRejectedValue(new ApiError('no analysis', { status: 404 }));

      render(
        <MemoryRouter>
          <HistoryPage />
        </MemoryRouter>
      );

      await waitFor(() => {
        expect(screen.getByText('Avg CPL')).toBeInTheDocument();
      });
      expect(summaryStat('Avg CPL')).toHaveTextContent('—');
      expect(summaryStat('Avg CPL')).not.toHaveTextContent('0');
    });

    it('renders a genuine Avg CPL of 0 as "0" — perfect play, not missing data', async () => {
      mockFetchHistory.mockResolvedValue([
        {
          ...UNANALYZED_HISTORY_RESPONSE[0],
          summary: {
            ...UNANALYZED_HISTORY_RESPONSE[0].summary,
            total_moves: 24,
            average_centipawn_loss: 0,
          },
        },
      ]);
      mockFetchAnalysis.mockRejectedValue(new ApiError('no analysis', { status: 404 }));

      render(
        <MemoryRouter>
          <HistoryPage />
        </MemoryRouter>
      );

      await waitFor(() => {
        expect(screen.getByText('Avg CPL')).toBeInTheDocument();
      });
      // A truthiness fallback (|| '—') would wrongly render an em-dash here.
      expect(summaryStat('Avg CPL')).toHaveTextContent('0');
    });
  });

  // g-22t8.2: the stats pane must read the SAME exact-best-projected moves the board
  // renders (and that GameAnalysisPage already feeds its own pane), or a promoted move
  // shows its gold "best" star while the pane beside it still counts it an inaccuracy.
  describe('exact-best projection before stats', () => {
    /** Player is white: a 40-CPL inaccuracy that IS the position best, plus a 20-CPL good. */
    const PROJECTION_ANALYSIS = (positionTrusted: boolean) => ({
      ...ANALYSIS_RESPONSE,
      moves: [
        {
          move_san: 'e4',
          move_uci: 'e2e4',
          fen_before: STARTING_FEN,
          fen_after: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1',
          color: 'white',
          classification: 'inaccuracy',
          eval_delta: 40,
        },
        {
          move_san: 'e5',
          move_uci: 'e7e5',
          fen_after: 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2',
          color: 'black',
          classification: 'good',
          eval_delta: 10,
        },
        {
          move_san: 'Nf3',
          move_uci: 'g1f3',
          fen_after: 'rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2',
          color: 'white',
          classification: 'good',
          eval_delta: 20,
        },
      ],
      position_analysis: {
        [STARTING_FEN]: {
          best_move_uci: 'e2e4',
          best_move_san: 'e4',
          best_move_eval_cp: 30,
          best_move_eval_mate: null,
          best_line_uci: ['e2e4', 'e7e5'],
          position_trusted: positionTrusted,
        },
      },
    });

    /** Player Avg CPL is the first of the two Avg CPL cells (player, then opponent). */
    const playerAvgCpl = (): Element =>
      document.querySelectorAll('.history-stats-pane__value--acpl')[0];

    /** The `moves` prop the board actually received, read back off the mock's rows. */
    const boardMoves = (): { classification: string | null; delta: string | null }[] =>
      screen.getAllByTestId('board-move').map((el) => ({
        classification: el.getAttribute('data-classification'),
        delta: el.getAttribute('data-delta'),
      }));

    it('promotes a played move equal to the TRUSTED position best before computing stats', async () => {
      mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
      mockFetchAnalysis.mockResolvedValue(PROJECTION_ANALYSIS(true));

      render(
        <MemoryRouter>
          <HistoryPage />
        </MemoryRouter>
      );

      await screen.findByTestId('analysis-board');

      // Promotion sets eval_delta to 0, NOT null — the move stays in the Avg CPL
      // denominator with a 0 numerator: (0 + 20) / 2 = 10, not 20.
      expect(screen.getByLabelText('Your Inaccuracies: 0')).toBeInTheDocument();
      expect(playerAvgCpl()).toHaveTextContent('10');

      // The board gets the same projected array, not raw analysis.moves: e4 promoted,
      // the other two untouched.
      expect(boardMoves()).toEqual([
        { classification: 'best', delta: '0' },
        { classification: 'good', delta: '10' },
        { classification: 'good', delta: '20' },
      ]);
    });

    it('leaves the move alone when the position is UNTRUSTED — and the stats say so', async () => {
      mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
      mockFetchAnalysis.mockResolvedValue(PROJECTION_ANALYSIS(false));

      render(
        <MemoryRouter>
          <HistoryPage />
        </MemoryRouter>
      );

      await screen.findByTestId('analysis-board');

      // Assert the VALUES, not merely "no promotion": the trust gate holding only
      // proves the untrusted best didn't drive the result, not that what we fell back
      // to is right. (40 + 20) / 2 = 30.
      expect(screen.getByLabelText('Your Inaccuracies: 1')).toBeInTheDocument();
      expect(playerAvgCpl()).toHaveTextContent('30');

      expect(boardMoves()).toEqual([
        { classification: 'inaccuracy', delta: '40' },
        { classification: 'good', delta: '10' },
        { classification: 'good', delta: '20' },
      ]);
    });
  });

  // g-22t8.3: the four poll outcomes must stay distinct — still-processing (active),
  // incomplete (terminal), stale (terminal, board survives) and analysis load failure
  // (which is NOT the page-level history error and must not blank the page).
  describe('poll outcomes', () => {
    const POLL_INTERVAL_MS = 2000;
    const POLL_MAX_ATTEMPTS = 60;

    /** An analysis the backend has not finished evaluating: no accuracy yet. */
    const INCOMPLETE = {
      ...ANALYSIS_RESPONSE,
      is_complete: false,
      summary: { ...ANALYSIS_RESPONSE.summary, accuracy: null },
    };

    const TWO_GAMES = [
      HISTORY_RESPONSE[0],
      {
        ...HISTORY_RESPONSE[0],
        session_id: 'def-456',
        result: 'resign',
        opening_name: 'French Defense',
      },
    ];

    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    /** Flush the pending fetches' microtasks without advancing the poll clock. */
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

    /** Pick the second game from the dropdown. fireEvent, not userEvent: userEvent's
     *  internal delay does not cooperate with the fake clock these tests depend on, and
     *  the selector's trigger and options are plain onClick handlers. */
    const selectSecondGame = async () => {
      fireEvent.click(screen.getByRole('button', { name: /Win vs 1500/ }));
      fireEvent.click(screen.getAllByRole('option')[1]);
      await settle();
    };

    const renderPage = () =>
      render(
        <MemoryRouter>
          <HistoryPage />
        </MemoryRouter>,
      );

    it('keeps the active processing notice while polls are still running', async () => {
      mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
      mockFetchAnalysis.mockResolvedValue(INCOMPLETE);

      renderPage();
      await settle();

      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      expect(screen.getByText(/Analysis still processing/)).toBeInTheDocument();
      expect(accuracyCell()).toHaveTextContent('computing');
      // Neither terminal notice may render while polling continues.
      expect(screen.queryByText(/Analysis incomplete/)).not.toBeInTheDocument();
      expect(screen.queryByText(/showing the last loaded result/)).not.toBeInTheDocument();
    });

    it('stays in the loading state through the retry window, claiming nothing about a payload it never got', async () => {
      mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
      mockFetchAnalysis.mockRejectedValue(new Error('Network error'));

      renderPage();
      await settle();

      expect(screen.getByText('Loading analysis...')).toBeInTheDocument();
      // "still processing" describes the PAYLOAD; there is no payload.
      expect(screen.queryByText(/Analysis still processing/)).not.toBeInTheDocument();
      expect(screen.queryByText(/Failed to load analysis/)).not.toBeInTheDocument();
      // ...and the history-row summary is not the final answer yet either.
      expect(screen.queryByText('Avg CPL')).not.toBeInTheDocument();

      expect(mockFetchAnalysis).toHaveBeenCalledTimes(1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      });
      expect(mockFetchAnalysis).toHaveBeenCalledTimes(2);
    });

    it('renders the board when a retry succeeds after a failed initial fetch', async () => {
      // Guards the unconditional setAnalysisLoading(false): the retry is not the initial
      // attempt, so an isInitial-guarded clear would leave the pane on "Loading
      // analysis..." forever with a good payload in hand.
      mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
      mockFetchAnalysis
        .mockRejectedValueOnce(new Error('Network error'))
        .mockResolvedValue(ANALYSIS_RESPONSE);

      renderPage();
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
      mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
      mockFetchAnalysis.mockResolvedValue(INCOMPLETE);

      renderPage();
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
      mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
      mockFetchAnalysis
        .mockResolvedValueOnce(INCOMPLETE)
        .mockRejectedValue(new Error('Network error'));

      renderPage();
      await settle();
      await exhaustPolls();

      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      expect(screen.getByText(/showing the last loaded result/)).toBeInTheDocument();
      expect(screen.queryByText(/Failed to load analysis/)).not.toBeInTheDocument();
      expect(screen.queryByText(/Analysis still processing/)).not.toBeInTheDocument();
    });

    it('keeps the board and goes stale when a refresh poll fails permanently', async () => {
      mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
      mockFetchAnalysis
        .mockResolvedValueOnce(INCOMPLETE)
        .mockRejectedValue(new ApiError('Game session not found', { status: 404 }));

      renderPage();
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

    it('surfaces an analysis load failure when no payload ever arrives', async () => {
      mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
      mockFetchAnalysis.mockRejectedValue(new Error('Network error'));

      renderPage();
      await settle();
      await exhaustPolls();

      expect(screen.getByText('Failed to load analysis')).toBeInTheDocument();
      expect(screen.queryByTestId('analysis-board')).not.toBeInTheDocument();
      expect(screen.queryByText(/showing the last loaded result/)).not.toBeInTheDocument();
    });

    it('stops polling immediately on a permanent error on the initial fetch', async () => {
      mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
      mockFetchAnalysis.mockRejectedValue(
        new ApiError('Game session not found', { status: 404 }),
      );

      renderPage();
      await settle();

      expect(screen.getByText('Game session not found')).toBeInTheDocument();
      expect(screen.queryByTestId('analysis-board')).not.toBeInTheDocument();

      await exhaustPolls();
      expect(mockFetchAnalysis).toHaveBeenCalledTimes(1);
    });

    it('clears a terminal notice when a different game is selected', async () => {
      mockFetchHistory.mockResolvedValue(TWO_GAMES);
      mockFetchAnalysis.mockImplementation((id: string) =>
        id === 'def-456' ? Promise.resolve(ANALYSIS_RESPONSE) : Promise.resolve(INCOMPLETE),
      );

      renderPage();
      await settle();
      await exhaustPolls();
      expect(screen.getByText(/Analysis incomplete/)).toBeInTheDocument();

      await selectSecondGame();

      expect(screen.queryByText(/Analysis incomplete/)).not.toBeInTheDocument();
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      expect(screen.queryByText(/Failed to load analysis/)).not.toBeInTheDocument();
    });

    // B2: the analysis error is NOT the history-list error. Writing it into the page-level
    // `error` hides the whole selected-game area — including, on a phone, the only game
    // selector — and selecting another game does not clear it. That is a dead end.
    describe('analysis failure does not blank the page', () => {
      it('keeps the list, the selector and the summary fallback, and reports in-pane', async () => {
        mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
        mockFetchAnalysis.mockRejectedValue(
          new ApiError('Game session not found', { status: 404 }),
        );

        renderPage();
        await settle();

        expect(screen.getByRole('button', { name: /Win vs 1500/ })).toBeInTheDocument();
        expect(screen.getByText('Avg CPL')).toBeInTheDocument();
        expect(document.querySelector('.history-shell__error')).toBeNull();

        const inPane = document.querySelector('.analysis-pane__error');
        expect(inPane).toHaveTextContent('Game session not found');
      });

      it('recovers when another game is selected', async () => {
        mockFetchHistory.mockResolvedValue(TWO_GAMES);
        mockFetchAnalysis.mockImplementation((id: string) =>
          id === 'def-456'
            ? Promise.resolve(ANALYSIS_RESPONSE)
            : Promise.reject(new ApiError('Game session not found', { status: 404 })),
        );

        renderPage();
        await settle();
        expect(screen.getByText('Game session not found')).toBeInTheDocument();

        await selectSecondGame();

        expect(screen.queryByText('Game session not found')).not.toBeInTheDocument();
        expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      });

      it('leaves a working selector in the pane on narrow screens, where the board carries it', async () => {
        setMatchMedia(true);
        mockFetchHistory.mockResolvedValue(TWO_GAMES);
        mockFetchAnalysis.mockImplementation((id: string) =>
          id === 'def-456'
            ? Promise.resolve(ANALYSIS_RESPONSE)
            : Promise.reject(new ApiError('Game session not found', { status: 404 })),
        );

        renderPage();
        await settle();

        // No board, so no mobileToolbar — the pane has to supply the selector itself.
        expect(screen.queryByTestId('analysis-board')).not.toBeInTheDocument();
        expect(
          document.querySelector('.analysis-pane__shell > .game-selector-row'),
        ).toBeInTheDocument();
        expect(screen.getAllByRole('button', { name: /Win vs 1500/ })).toHaveLength(1);

        await selectSecondGame();

        expect(screen.queryByText('Game session not found')).not.toBeInTheDocument();
        expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      });
    });
  });
});
