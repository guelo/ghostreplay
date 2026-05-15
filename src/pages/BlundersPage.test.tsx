import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
const { mockFetchBlunders, mockFetchAnalysis, mockAnalysisBoard } = vi.hoisted(() => ({
  mockFetchBlunders: vi.fn(),
  mockFetchAnalysis: vi.fn(),
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

vi.mock('react-chessboard', () => ({
  Chessboard: () => <div data-testid="chessboard" />,
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
    pass_streak: 0,
    last_reviewed_at: null,
    created_at: '2026-04-20T12:00:00Z',
    last_played_at: '2026-04-21T12:00:00Z',
    opportunities_since_review: 0,
    opportunities_30d: 0,
    reached_30d: 0,
    p_reach: 0.5,
  },
];

const blunderEnvelope = (items = BLUNDERS_RESPONSE, total = items.length, dueTotal: number | null = null) => ({
  items,
  total,
  due_total: dueTotal,
  limit: 50,
  offset: 0,
  due: false,
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

    expect(mockFetchBlunders).toHaveBeenCalledWith({ due: false, limit: 50, offset: 0 });
    expect(mockFetchAnalysis).not.toHaveBeenCalled();
    expect(screen.getByText('Select a blunder to study.')).toBeTruthy();
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
    expect(mockFetchBlunders).toHaveBeenLastCalledWith({ due: false, limit: 50, offset: 1 });
  });

  it('resets selection and count display when toggling due mode', async () => {
    mockFetchBlunders
      .mockResolvedValueOnce(blunderEnvelope(BLUNDERS_RESPONSE, 4, null))
      .mockResolvedValueOnce({
        ...blunderEnvelope(BLUNDERS_RESPONSE, 1, 1),
        due: true,
      });

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('4 total');
    expect(screen.queryByText('1 due')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Due only' }));

    await screen.findByText('1 of 1 due');
    expect(mockFetchBlunders).toHaveBeenLastCalledWith({ due: true, limit: 50, offset: 0 });
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
        due: true,
      });

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await screen.findByText('Bc4');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    fireEvent.click(screen.getByRole('button', { name: 'Due only' }));
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
