import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import {
  buildEvidenceRow,
  useAnalysisEvidence,
  EVIDENCE_SEARCH_DEPTH,
} from './analysisEvidence'
import type { AnalysisWorkerResponse } from '../workers/analysisMessages'

const submitAnalysisEvidenceMock = vi.fn()

vi.mock('../utils/api', () => ({
  submitAnalysisEvidence: (...args: unknown[]) => submitAnalysisEvidenceMock(...args),
}))

type MessageHandler = (event: MessageEvent) => void
let messageHandler: MessageHandler | null = null
let workerConstructed = 0
const postMessageMock = vi.fn()
const terminateMock = vi.fn()

function MockWorker() {
  workerConstructed += 1
  // @ts-expect-error -- mock constructor
  this.postMessage = postMessageMock
  // @ts-expect-error -- mock constructor
  this.addEventListener = vi.fn((type: string, handler: MessageHandler) => {
    if (type === 'message') messageHandler = handler
  })
  // @ts-expect-error -- mock constructor
  this.removeEventListener = vi.fn()
  // @ts-expect-error -- mock constructor
  this.terminate = terminateMock
}

vi.stubGlobal('Worker', MockWorker)

const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
// Black to move (after 1. e4).
const AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'

type AnalysisMsg = Extract<AnalysisWorkerResponse, { type: 'analysis' }>

const analysisMsg = (overrides: Partial<AnalysisMsg> = {}): AnalysisMsg => ({
  type: 'analysis',
  id: 'x',
  move: 'e2e4',
  bestMove: 'e2e4',
  bestLine: ['e2e4', 'e7e5'],
  bestEval: 30,
  playedEval: 30,
  bestEvalMate: null,
  playedEvalMate: null,
  delta: 0,
  classification: 'best',
  canonical: true,
  ...overrides,
})

// --------------------------------------------------------------------------- #
// buildEvidenceRow
// --------------------------------------------------------------------------- #
describe('buildEvidenceRow', () => {
  it('white to move: evals stay white-relative, delta recomputed', () => {
    const row = buildEvidenceRow(START, 'e2e4', analysisMsg({ bestEval: 40, playedEval: 25 }))
    expect(row).not.toBeNull()
    expect(row!.played_eval).toBe(25)
    expect(row!.best_eval).toBe(40)
    // white to move: delta = best - played = 15.
    expect(row!.eval_delta).toBe(15)
    expect(row!.best_move_uci).toBe('e2e4')
    expect(row!.best_line_uci).toEqual(['e2e4', 'e7e5'])
    expect(row!.classification).toBe('best')
  })

  it('black to move: converts mover-relative to white-relative and recomputes delta', () => {
    // mover=black. best is good for black (+50 mover) -> white -50; played -20 mover -> white +20.
    const row = buildEvidenceRow(
      AFTER_E4,
      'e7e5',
      analysisMsg({ move: 'e7e5', bestMove: 'e7e5', bestEval: 50, playedEval: -20, delta: -999 }),
    )
    expect(row).not.toBeNull()
    expect(row!.best_eval).toBe(-50)
    expect(row!.played_eval).toBe(20)
    // black to move: delta = whitePlayed - whiteBest = 20 - (-50) = 70. message.delta ignored.
    expect(row!.eval_delta).toBe(70)
  })

  it('clamps a negative recomputed delta to zero', () => {
    // white to move, played (100) better than "best" (50) -> raw delta negative.
    const row = buildEvidenceRow(START, 'e2e4', analysisMsg({ bestEval: 50, playedEval: 100 }))
    expect(row!.eval_delta).toBe(0)
  })

  it('converts mate counts to white-relative', () => {
    const row = buildEvidenceRow(
      AFTER_E4,
      'e7e5',
      analysisMsg({
        move: 'e7e5',
        bestMove: 'e7e5',
        bestEval: 31900,
        playedEval: 31900,
        bestEvalMate: 2,
        playedEvalMate: 2,
      }),
    )
    // black mover -> white-relative mate sign flips.
    expect(row!.best_eval_mate).toBe(-2)
    expect(row!.played_eval_mate).toBe(-2)
  })

  it('drops a non-canonical result', () => {
    expect(buildEvidenceRow(START, 'e2e4', analysisMsg({ canonical: false }))).toBeNull()
  })

  it('drops a one-move PV', () => {
    expect(buildEvidenceRow(START, 'e2e4', analysisMsg({ bestLine: ['e2e4'] }))).toBeNull()
  })

  it('drops a null bestMove / (none)', () => {
    expect(buildEvidenceRow(START, 'e2e4', analysisMsg({ bestMove: '(none)' }))).toBeNull()
  })

  it('drops when a required eval is null', () => {
    expect(buildEvidenceRow(START, 'e2e4', analysisMsg({ playedEval: null }))).toBeNull()
    expect(buildEvidenceRow(START, 'e2e4', analysisMsg({ bestEval: null }))).toBeNull()
  })

  it('drops when classification is null', () => {
    expect(buildEvidenceRow(START, 'e2e4', analysisMsg({ classification: null }))).toBeNull()
  })
})

// --------------------------------------------------------------------------- #
// useAnalysisEvidence
// --------------------------------------------------------------------------- #
const postedAnalyzeMove = () =>
  postMessageMock.mock.calls.map(([m]) => m).find((m) => m?.type === 'analyze-move')

describe('useAnalysisEvidence', () => {
  beforeEach(() => {
    postMessageMock.mockClear()
    terminateMock.mockClear()
    submitAnalysisEvidenceMock.mockReset()
    submitAnalysisEvidenceMock.mockResolvedValue([])
    messageHandler = null
    workerConstructed = 0
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('no-ops entirely when sessionId is absent', () => {
    const { result } = renderHook(() => useAnalysisEvidence(undefined))
    act(() => result.current.requestEvidence(START, 'e2e4', 'white'))
    expect(workerConstructed).toBe(0)
    expect(postMessageMock).not.toHaveBeenCalled()
    expect(submitAnalysisEvidenceMock).not.toHaveBeenCalled()
  })

  it('posts an analyze-move at the evidence depth and submits the mapped row', async () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    expect(workerConstructed).toBe(1)

    act(() => result.current.requestEvidence(START, 'e2e4', 'white'))
    const msg = postedAnalyzeMove()
    expect(msg).toMatchObject({
      type: 'analyze-move',
      fen: START,
      move: 'e2e4',
      playerColor: 'white',
      depth: EVIDENCE_SEARCH_DEPTH,
    })

    await act(async () => {
      messageHandler?.({ data: analysisMsg({ id: msg.id }) } as MessageEvent)
    })
    expect(submitAnalysisEvidenceMock).toHaveBeenCalledTimes(1)
    const [sessionId, rows] = submitAnalysisEvidenceMock.mock.calls[0]
    expect(sessionId).toBe('s1')
    expect(rows[0]).toMatchObject({ fen: START, move_uci: 'e2e4', best_move_uci: 'e2e4' })
  })

  it('does not submit a non-canonical result', async () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    act(() => result.current.requestEvidence(START, 'e2e4', 'white'))
    const msg = postedAnalyzeMove()
    await act(async () => {
      messageHandler?.({ data: analysisMsg({ id: msg.id, canonical: false }) } as MessageEvent)
    })
    expect(submitAnalysisEvidenceMock).not.toHaveBeenCalled()
  })

  it('dedupes a completed (fen, move) — no second analyze-move', async () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    act(() => result.current.requestEvidence(START, 'e2e4', 'white'))
    const msg = postedAnalyzeMove()
    await act(async () => {
      messageHandler?.({ data: analysisMsg({ id: msg.id }) } as MessageEvent)
    })
    postMessageMock.mockClear()
    act(() => result.current.requestEvidence(START, 'e2e4', 'white'))
    expect(postMessageMock).not.toHaveBeenCalled()
  })

  it('cancel prevents an interrupted search from submitting', async () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    act(() => result.current.requestEvidence(START, 'e2e4', 'white'))
    const msg = postedAnalyzeMove()
    act(() => result.current.cancel())
    expect(postMessageMock).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'cancel-analysis', id: msg.id }),
    )
    // A late analysis for the canceled id must NOT submit.
    await act(async () => {
      messageHandler?.({ data: analysisMsg({ id: msg.id }) } as MessageEvent)
    })
    expect(submitAnalysisEvidenceMock).not.toHaveBeenCalled()
  })

  it('cancels the in-flight search when a new move is requested (one at a time)', () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    act(() => result.current.requestEvidence(START, 'e2e4', 'white'))
    const first = postedAnalyzeMove()
    act(() => result.current.requestEvidence(AFTER_E4, 'e7e5', 'black'))
    expect(postMessageMock).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'cancel-analysis', id: first.id }),
    )
  })
})
