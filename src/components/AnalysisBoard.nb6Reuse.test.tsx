import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '../test/utils'
import AnalysisBoard from './AnalysisBoard'
import type { AnalysisMove, MoveUpgrade } from '../utils/api'
import type { CompletedRootAnalysis } from '../workers/stockfishMessages'

/**
 * Motivating end-to-end reuse regression (g-nb6-reuse-integration).
 *
 * Game aa05b29f-4409-4713-bd9b-719cdefcdb68, position after 21...h4, played move
 * 22.Nb6+ (c4b6). Before g-reuse-d21-search the board ran a SECOND, hidden depth-21
 * single-PV search for evidence; its root chose a5a6 while its post-move scores
 * rated c4b6 higher, so MoveList kept saying "excellent" beside a visible line 1 of
 * Nb6+. This drives the real component with the real evidence layer and the real
 * `submitAnalysisEvidence`, stubbing only the engine hooks and `fetch`, and asserts
 * the completed VISIBLE MultiPV snapshot alone produces the upgrade.
 *
 * The backend half (rederivation, corrective replacement of the stored
 * browser-analysis-v1 row, durable refetch) lives in
 * backend/test_analysis_evidence_api.py::test_motivating_nb6_visible_multipv_corrects_the_contradictory_row.
 */

const NB6_FEN_BEFORE = '2kr1b1r/pp1q4/2p5/P1P2p2/2NP2pp/8/1B3PPP/R2QR1K1 w - - 0 22'
const NB6_FEN_AFTER = '2kr1b1r/pp1q4/1Np5/P1P2p2/3P2pp/8/1B3PPP/R2QR1K1 b - - 1 22'
const A5_FEN_BEFORE = '2kr1b1r/pp1q4/2p5/2P2p1p/P1NP2p1/8/1B3PPP/R2QR1K1 w - - 0 21'
const A5_FEN_AFTER = '2kr1b1r/pp1q4/2p5/P1P2p1p/2NP2p1/8/1B3PPP/R2QR1K1 b - - 0 21'
const SESSION_ID = 'aa05b29f-4409-4713-bd9b-719cdefcdb68'

// The completed, unrestricted depth-21 MultiPV-3 snapshot the visible search
// produces at this position: it ranks c4b6 FIRST (the ordering the user sees),
// ahead of a5a6 and Qb3. Scores are side-to-move (white) relative.
const NB6_SNAPSHOT: CompletedRootAnalysis = {
  requestId: 'nb6-1',
  fen: NB6_FEN_BEFORE,
  bestMove: 'c4b6',
  lines: [
    { depth: 21, multipv: 1, pv: ['c4b6', 'a7b6', 'a5b6'], score: { type: 'cp', value: 631 } },
    { depth: 21, multipv: 2, pv: ['a5a6', 'h4h3', 'a6b7'], score: { type: 'cp', value: 541 } },
    { depth: 21, multipv: 3, pv: ['d1b3', 'h4h3', 'b3a4'], score: { type: 'cp', value: 521 } },
  ],
  limit: { type: 'depth', value: 21 },
  multipv: 3,
  searchmoves: null,
}

// --- engine hooks: the ONLY search in this test is the visible one ------------
// Hoisted so the `vi.mock` factories below (which run before this module body) can
// close over them; the resolved value is set per-test in `beforeEach`.
const { mockEvaluatePosition, mockStopSearch, mockAnalyzeMove } = vi.hoisted(() => ({
  mockEvaluatePosition: vi.fn(),
  mockStopSearch: vi.fn(),
  mockAnalyzeMove: vi.fn(() => 'req-1'),
}))

vi.mock('../hooks/useStockfishEngine', () => ({
  useStockfishEngine: () => ({
    info: [],
    infoFen: null,
    isThinking: false,
    evaluatePosition: mockEvaluatePosition,
    stopSearch: mockStopSearch,
  }),
}))

vi.mock('../hooks/useMoveAnalysis', () => ({
  useMoveAnalysis: () => ({
    analyzeMove: mockAnalyzeMove,
    analysisMap: new Map(),
    lastAnalysis: null,
    clearAnalysis: vi.fn(),
  }),
}))

// --- presentation stubs (this test asserts data flow, not chrome) -------------
vi.mock('react-chessboard', () => ({
  Chessboard: () => <div data-testid="chessboard" />,
  defaultArrowOptions: {},
}))
vi.mock('./EvalBar', () => ({ default: () => <div /> }))
vi.mock('./AnalysisGraph', () => ({ default: () => <div /> }))
vi.mock('./MaterialDisplay', () => ({ default: () => <div /> }))

const { capturedMoveListRef } = vi.hoisted(() => ({
  capturedMoveListRef: { current: {} as Record<string, unknown> },
}))
const captureMoveList = (props: Record<string, unknown>) => {
  capturedMoveListRef.current = props
  return <div data-testid="move-list" />
}
vi.mock('./MoveList', () => ({ default: (p: Record<string, unknown>) => captureMoveList(p) }))
vi.mock('./HorizontalMoveList', () => ({
  default: (p: Record<string, unknown>) => captureMoveList(p),
}))

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

// Any Worker construction at all is a regression: both worker-owning hooks are
// stubbed above, so the reuse layer is the only thing that could spawn one — and
// reusing the completed visible search means it must never need to.
const workerSpy = vi.fn()
vi.stubGlobal(
  'Worker',
  class {
    constructor(...args: unknown[]) {
      workerSpy(...args)
    }
  },
)

const NB6_UPGRADE: MoveUpgrade = {
  classification: 'best',
  eval_cp: 631,
  eval_mate: null,
  best_move_san: 'Nb6+',
  best_move_eval_cp: 631,
  eval_delta: 0,
  authoritative: false,
}

// Moves 21.a5 / 21...h4 / 22.Nb6+ with their real wire fields. Index 0 is a white
// move so the list's index-parity perspective conversion matches the real game.
const moves: AnalysisMove[] = [
  {
    move_number: 21, color: 'white', move_san: 'a5',
    fen_before: A5_FEN_BEFORE, move_uci: 'a4a5', fen_after: A5_FEN_AFTER,
    eval_cp: 506, eval_mate: null, best_move_san: 'a5', best_move_eval_cp: 506,
    eval_delta: 0, classification: 'best',
  },
  {
    move_number: 21, color: 'black', move_san: 'h4',
    fen_before: A5_FEN_AFTER, move_uci: 'h5h4', fen_after: NB6_FEN_BEFORE,
    eval_cp: -497, eval_mate: null, best_move_san: 'g3', best_move_eval_cp: -520,
    eval_delta: 0, classification: 'excellent',
  },
  {
    // The motivating move: graded 'excellent' at game time with a5a6 as best.
    move_number: 22, color: 'white', move_san: 'Nb6+',
    fen_before: NB6_FEN_BEFORE, move_uci: 'c4b6', fen_after: NB6_FEN_AFTER,
    eval_cp: 593, eval_mate: null, best_move_san: 'a6', best_move_eval_cp: 516,
    eval_delta: 0, classification: 'excellent',
  },
]

const nb6Badge = () =>
  (capturedMoveListRef.current.moves as Array<{ classification: string | null }>)[2]
    .classification

const postBody = () => JSON.parse(fetchMock.mock.calls[0][1].body as string)

describe('AnalysisBoard — motivating 22.Nb6+ reuse regression (g-nb6-reuse-integration)', () => {
  beforeEach(() => {
    capturedMoveListRef.current = {}
    vi.clearAllMocks()
    mockEvaluatePosition.mockResolvedValue({
      move: 'c4b6',
      raw: 'bestmove c4b6',
      snapshot: NB6_SNAPSHOT,
    })
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({
        results: [
          {
            fen: NB6_FEN_BEFORE,
            move_uci: 'c4b6',
            reason: 'protocol_corrected_replace',
            upgrade: NB6_UPGRADE,
          },
        ],
      }),
    })
  })

  const renderBoard = () =>
    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        startingFen={A5_FEN_BEFORE}
        sessionId={SESSION_ID}
        initialMoveIndex={1}
      />,
    )

  it('derives the evidence row from the completed visible MultiPV snapshot alone', async () => {
    renderBoard()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain(`/api/session/${SESSION_ID}/analysis-evidence`)
    expect(init.method).toBe('POST')
    // The endpoint-controlled producer discriminator: this bundle came from the
    // visible search, not the retired hidden analyzer.
    expect(postBody().producer).toBe('visible-multipv-v1')

    // c4b6 is BOTH the played and the best move, from ONE search: no contradiction
    // between the displayed line 1 and the persisted root winner.
    expect(postBody().rows).toEqual([
      {
        fen: NB6_FEN_BEFORE,
        move_uci: 'c4b6',
        best_move_uci: 'c4b6',
        best_line_uci: ['c4b6', 'a7b6', 'a5b6'],
        played_eval: 631,
        played_eval_mate: null,
        best_eval: 631,
        best_eval_mate: null,
        eval_delta: 0,
        classification: 'best',
      },
    ])
  })

  it('runs exactly one root search and no hidden analyzer request', async () => {
    renderBoard()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    // One VISIBLE unrestricted depth-21 MultiPV-3 root search, and nothing else.
    expect(mockEvaluatePosition).toHaveBeenCalledTimes(1)
    expect(mockEvaluatePosition).toHaveBeenCalledWith(NB6_FEN_BEFORE, {
      depth: 21,
      multipv: 3,
    })
    // No second worker, no analyze-move request, no post-played/post-best search.
    expect(workerSpy).not.toHaveBeenCalled()
    expect(mockAnalyzeMove).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledTimes(1) // the evidence POST is the only call
  })

  it('patches the open MoveList from excellent to best on the accepted write', async () => {
    renderBoard()
    expect(nb6Badge()).toBe('excellent')
    await waitFor(() => expect(nb6Badge()).toBe('best'))
  })
})
