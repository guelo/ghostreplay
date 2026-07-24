import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
const { mockFetchBlunders, mockFetchAnalysis, mockLookupOpeningByFen, mockAnalysisBoard } = vi.hoisted(() => ({
  mockFetchBlunders: vi.fn(),
  mockFetchAnalysis: vi.fn(),
  mockLookupOpeningByFen: vi.fn(),
  mockAnalysisBoard: vi.fn(
    ({ initialMoveIndex }: { initialMoveIndex?: number }) => (
    <div
      data-testid="analysis-board"
      data-initial-move={initialMoveIndex === undefined ? 'undefined' : initialMoveIndex}
    />
  ),
  ),
}));

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
  default: mockAnalysisBoard,
}));

vi.mock('../openings/openingBook', () => ({
  lookupOpeningByFen: (...args: unknown[]) => mockLookupOpeningByFen(...args),
}));

vi.mock('react-chessboard', () => ({
  Chessboard: () => <div data-testid="chessboard" />,
}));

// Mock AppNav
vi.mock('../components/AppNav', () => ({
  default: () => <nav data-testid="app-nav" />,
}));

const captureEventMock = vi.fn();
vi.mock('../analytics/posthog', () => ({
  captureEvent: (...args: unknown[]) => captureEventMock(...args),
}));

const BLUNDERS_RESPONSE = [
  {
    id: 1,
    fen: 'r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3',
    bad_move: 'Bc4',
    best_move: 'Bb5',
    eval_loss_cp: 100,
    opening_family: 'Italian Game' as string | null,
    srs_priority: 1.5,
    srs_due: true,
    ghost_eligible: true,
    practice_priority_score: 2.3,
    review_count: 0,
    pass_count: 0,
    fail_count: 0,
    last_result: null as boolean | null,
    source_session_id: 'session-123' as string | null,
    last_session_id: 'session-123',
    pass_streak: 0,
    last_reviewed_at: null,
    created_at: '2026-04-20T12:00:00Z',
    last_played_at: '2026-04-21T12:00:00Z',
    opportunities_since_review: 0,
    opportunities_30d: 0,
    reached_30d: 0,
    reached_since_review: 0,
    p_reach: 0.5,
  },
];

const blunderEnvelope = (
  items = BLUNDERS_RESPONSE,
  total = items.length,
  practiceReadyTotal: number | null = null,
) => ({
  items,
  total,
  due_total: null,
  practice_ready_total: practiceReadyTotal,
  limit: 50,
  offset: 0,
  due: false,
  practice_ready: false,
});

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
    mockLookupOpeningByFen.mockResolvedValue(null);
  });

  it('passes correct initialMoveIndex when move is found in analysis', async () => {
    mockFetchBlunders.mockResolvedValue(blunderEnvelope());
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    expect(screen.getByText(/Last Played:/)).toBeInTheDocument();
    expect(mockFetchAnalysis).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('option', { selected: false }));

    await waitFor(() => expect(mockFetchAnalysis).toHaveBeenCalledWith('session-123'));
    await waitFor(() => expect(mockAnalysisBoard).toHaveBeenCalled());

    // The blunder FEN matches the position BEFORE Bc4 (index 4)
    expect(mockAnalysisBoard).toHaveBeenLastCalledWith(
      expect.objectContaining({ initialMoveIndex: 4 }),
      undefined,
    );
  });

  it('captures blunder_selected with the active filter when a card is clicked', async () => {
    mockFetchBlunders.mockResolvedValue(blunderEnvelope());
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    fireEvent.click(screen.getByRole('option', { selected: false }));

    expect(captureEventMock).toHaveBeenCalledWith('blunder_selected', {
      blunder_id: 1,
      filter: 'all',
    });

    // Selecting the card kicks off an async analysis fetch that updates state
    // after this assertion; flush it inside act() so it doesn't leak past the
    // test as a "not wrapped in act(...)" warning.
    await act(async () => {
      await Promise.resolve();
    });
  });

  it('displays UCI best moves as algebraic notation on blunder cards', async () => {
    mockFetchBlunders.mockResolvedValue(
      blunderEnvelope([
        {
          ...BLUNDERS_RESPONSE[0],
          best_move: 'f1b5',
        },
      ]),
    );

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bb5');
    expect(screen.queryByText('f1b5')).not.toBeInTheDocument();
  });

  it('displays resolved opening names on blunder cards and details', async () => {
    mockFetchBlunders.mockResolvedValue(blunderEnvelope());
    mockLookupOpeningByFen.mockResolvedValue({
      eco: 'C50',
      name: 'Italian Game',
      source: 'eco',
    });

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('C50 Italian Game');
    fireEvent.click(screen.getByRole('option', { selected: false }));

    expect(await screen.findAllByText('C50 Italian Game')).toHaveLength(2);
    expect(mockLookupOpeningByFen).toHaveBeenCalledWith(BLUNDERS_RESPONSE[0].fen);
  });

  it('falls back to stored opening family when exact lookup misses', async () => {
    mockFetchBlunders.mockResolvedValue(blunderEnvelope());
    mockLookupOpeningByFen.mockResolvedValue(null);

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Italian Game');
  });

  it('derives the selected opening from source game history when the blunder position is off-book', async () => {
    mockFetchBlunders.mockResolvedValue(
      blunderEnvelope([
        {
          ...BLUNDERS_RESPONSE[0],
          opening_family: null,
        },
      ]),
    );
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);
    mockLookupOpeningByFen.mockImplementation((fen: string) => {
      if (fen === ANALYSIS_RESPONSE.moves[2].fen_after) {
        return Promise.resolve({
          eco: 'C50',
          name: 'Italian Game',
          source: 'eco',
        });
      }
      return Promise.resolve(null);
    });

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    await screen.findByText('C50 Italian Game');
    fireEvent.click(screen.getByRole('option', { selected: false }));

    expect(await screen.findAllByText('C50 Italian Game')).toHaveLength(2);
  });

  it('loads the source session when a later review session exists', async () => {
    mockFetchBlunders.mockResolvedValue(
      blunderEnvelope([
        {
          ...BLUNDERS_RESPONSE[0],
          source_session_id: 'source-session',
          last_session_id: 'review-session',
        },
      ]),
    );
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    fireEvent.click(screen.getByRole('option', { selected: false }));

    await waitFor(() => expect(mockFetchAnalysis).toHaveBeenCalledWith('source-session'));
    await waitFor(() => expect(mockAnalysisBoard).toHaveBeenCalled());
    expect(mockAnalysisBoard).toHaveBeenLastCalledWith(
      // The evidence driver session is the same source session the board renders.
      expect.objectContaining({ initialMoveIndex: 4, sessionId: 'source-session' }),
      undefined,
    );
  });

  it('loads review session analysis for the study board when source session is missing', async () => {
    mockFetchBlunders.mockResolvedValue(
      blunderEnvelope([
        {
          ...BLUNDERS_RESPONSE[0],
          source_session_id: null,
          last_session_id: 'review-session',
        },
      ]),
    );
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    fireEvent.click(screen.getByRole('option', { selected: false }));

    await waitFor(() => expect(mockFetchAnalysis).toHaveBeenCalledWith('review-session'));
    await waitFor(() => expect(mockAnalysisBoard).toHaveBeenCalled());
    // Falls back to the review session id when there is no source session.
    expect(mockAnalysisBoard).toHaveBeenLastCalledWith(
      expect.objectContaining({ sessionId: 'review-session' }),
      undefined,
    );
  });

  it('retries cancelled background opening derivation requests', async () => {
    let resolveFirstAnalysis: (value: unknown) => void = () => {};
    const unresolved = {
      ...BLUNDERS_RESPONSE[0],
      opening_family: null,
    };
    mockFetchBlunders
      .mockResolvedValueOnce(blunderEnvelope([unresolved], 1))
      .mockResolvedValueOnce({
        ...blunderEnvelope([unresolved], 1, 1),
        practice_ready: true,
      });
    mockFetchAnalysis
      .mockReturnValueOnce(new Promise((resolve) => { resolveFirstAnalysis = resolve; }))
      .mockResolvedValueOnce(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    await waitFor(() => expect(mockFetchAnalysis).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: 'Practice-ready' }));
    await screen.findByText('1 of 1 ready');

    await waitFor(() => expect(mockFetchAnalysis).toHaveBeenCalledTimes(2));
    expect(mockFetchAnalysis).toHaveBeenNthCalledWith(1, 'session-123');
    expect(mockFetchAnalysis).toHaveBeenNthCalledWith(2, 'session-123');

    resolveFirstAnalysis(ANALYSIS_RESPONSE);
  });

  it('matches blunder positions when only a non-legal en passant field differs', async () => {
    mockFetchBlunders.mockResolvedValue(
      blunderEnvelope([
        {
          ...BLUNDERS_RESPONSE[0],
          fen: 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',
          bad_move: 'Nf3',
        },
      ]),
    );
    mockFetchAnalysis.mockResolvedValue({
      ...ANALYSIS_RESPONSE,
      moves: ANALYSIS_RESPONSE.moves.slice(0, 3),
    });

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Nf3');
    fireEvent.click(screen.getByRole('option', { selected: false }));

    await waitFor(() => expect(mockAnalysisBoard).toHaveBeenCalled());
    expect(mockAnalysisBoard).toHaveBeenLastCalledWith(
      expect.objectContaining({ initialMoveIndex: 2 }),
      undefined,
    );
  });

  it('falls back to undefined (latest) when move is not found in analysis', async () => {
    mockFetchBlunders.mockResolvedValue(blunderEnvelope());
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

    await screen.findByText('Bc4');
    fireEvent.click(screen.getByRole('option', { selected: false }));

    await waitFor(() => expect(mockFetchAnalysis).toHaveBeenCalledWith('session-123'));
    await waitFor(() => expect(mockAnalysisBoard).toHaveBeenCalled());

    expect(mockAnalysisBoard).toHaveBeenLastCalledWith(
      expect.objectContaining({ initialMoveIndex: undefined }),
      undefined,
    );
  });

  it('loads the first page without auto-selecting a blunder', async () => {
    mockFetchBlunders.mockResolvedValue(blunderEnvelope());

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');

    expect(mockFetchBlunders).toHaveBeenCalledWith({ practiceReady: false, limit: 50, offset: 0 });
    expect(mockFetchAnalysis).not.toHaveBeenCalled();
    expect(screen.getByText('Select a blunder to study.')).toBeTruthy();
  });

  it('shows pass/fail counters and recent pass chip for a reviewed blunder', async () => {
    mockFetchBlunders.mockResolvedValue(
      blunderEnvelope([
        {
          ...BLUNDERS_RESPONSE[0],
          review_count: 3,
          pass_count: 2,
          fail_count: 1,
          last_result: true,
        },
      ]),
    );
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    fireEvent.click(screen.getByRole('option', { selected: false }));

    expect(await screen.findByText('2/1')).toBeInTheDocument();
    expect(screen.getByText('3 reviews')).toBeInTheDocument();
    expect(screen.getByText('Pass')).toBeInTheDocument();
  });

  it('shows a fail chip when the most recent review failed', async () => {
    mockFetchBlunders.mockResolvedValue(
      blunderEnvelope([
        {
          ...BLUNDERS_RESPONSE[0],
          review_count: 2,
          pass_count: 1,
          fail_count: 1,
          last_result: false,
        },
      ]),
    );
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    fireEvent.click(screen.getByRole('option', { selected: false }));

    expect(await screen.findByText('Fail')).toBeInTheDocument();
  });

  it('shows an em dash and "Not reviewed" for a never-reviewed blunder', async () => {
    mockFetchBlunders.mockResolvedValue(blunderEnvelope());
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    fireEvent.click(screen.getByRole('option', { selected: false }));

    expect(await screen.findByText('—')).toBeInTheDocument();
    expect(screen.getByText('Not reviewed')).toBeInTheDocument();
  });

  it('appends more blunders when Load more is clicked', async () => {
    const first = BLUNDERS_RESPONSE[0];
    const second = { ...first, id: 2, bad_move: 'Qh5', best_move: 'Nf6' };
    mockFetchBlunders
      .mockResolvedValueOnce(blunderEnvelope([first], 2))
      .mockResolvedValueOnce({
        ...blunderEnvelope([second], 2),
        offset: 1,
      });

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));

    await screen.findByText('Qh5');
    expect(mockFetchBlunders).toHaveBeenLastCalledWith({ practiceReady: false, limit: 50, offset: 1 });
  });

  it('resets selection and count display when toggling practice-ready mode', async () => {
    mockFetchBlunders
      .mockResolvedValueOnce(blunderEnvelope(BLUNDERS_RESPONSE, 4, 1))
      .mockResolvedValueOnce({
        ...blunderEnvelope(BLUNDERS_RESPONSE, 1, 1),
        practice_ready: true,
      });

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('4 total');
    expect(screen.getByText('1 ready')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Practice-ready' }));

    await screen.findByText('1 of 1 ready');
    expect(mockFetchBlunders).toHaveBeenLastCalledWith({ practiceReady: true, limit: 50, offset: 0 });
    expect(mockFetchAnalysis).not.toHaveBeenCalled();
  });

  it('ignores stale load-more responses after due mode changes', async () => {
    let resolveLoadMore: (value: unknown) => void = () => {};
    const first = BLUNDERS_RESPONSE[0];
    const stale = { ...first, id: 2, bad_move: 'Qh5', best_move: 'Nf6' };
    const dueItem = { ...first, id: 3, bad_move: 'Nxd5', best_move: 'Qxd5' };

    mockFetchBlunders
      .mockResolvedValueOnce(blunderEnvelope([first], 2))
      .mockReturnValueOnce(new Promise((resolve) => { resolveLoadMore = resolve; }))
      .mockResolvedValueOnce({
        ...blunderEnvelope([dueItem], 1, 1),
        practice_ready: true,
      });

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    fireEvent.click(screen.getByRole('button', { name: 'Practice-ready' }));
    await screen.findByText('Nxd5');

    resolveLoadMore({
      ...blunderEnvelope([stale], 2),
      offset: 1,
    });

    await waitFor(() => {
      expect(screen.queryByText('Qh5')).toBeNull();
    });
  });
});
