import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import {
  DecisionOwner,
  type DecisionOwnerGameState,
  type DecisionOwnerCallbacks,
  type PendingAnalysisContext,
  type PendingSrsReview,
} from './DecisionOwner'
import type { AnalysisOutcome } from './GameAnalysisCoordinator'
import type { AnalysisResult } from '../hooks/useMoveAnalysis'
import { ApiError } from '../utils/api'

// Spy the two POST endpoints; keep ApiError/errorCodeOf real so retry
// classification is exercised against the production contract.
const recordBlunderMock = vi.fn()
const reviewSrsBlunderMock = vi.fn()
vi.mock('../utils/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/api')>()
  return {
    ...actual,
    recordBlunder: (...args: unknown[]) => recordBlunderMock(...args),
    reviewSrsBlunder: (...args: unknown[]) => reviewSrsBlunderMock(...args),
  }
})

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

let seq = 0

const makeResult = (overrides: Partial<AnalysisResult> = {}): AnalysisResult => ({
  id: overrides.id ?? `req-${seq++}`,
  move: 'e2e4',
  bestMove: 'd2d4',
  bestLine: null,
  bestEval: 0,
  playedEval: 0,
  currentPositionEval: 0,
  playedEvalMate: null,
  currentPositionEvalMate: null,
  moveIndex: 0,
  delta: 0,
  classification: 'good',
  blunder: false,
  recordable: false,
  ...overrides,
})

const makeContext = (
  moveIndex: number,
  overrides: Partial<PendingAnalysisContext> = {},
): PendingAnalysisContext => ({
  fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
  pgn: '1. e4',
  moveSan: 'e4',
  moveUci: 'e2e4',
  moveIndex,
  ...overrides,
})

const makeSrsReview = (
  moveIndex: number,
  overrides: Partial<PendingSrsReview> = {},
): PendingSrsReview => ({
  sessionId: 'sess-1',
  blunderId: 100,
  moveIndex,
  userMoveSan: 'e4',
  srs: null,
  srsDecisionId: `srs-${seq++}`,
  ...overrides,
})

function makeOwner(stateOverrides: Partial<DecisionOwnerGameState> = {}) {
  const gameState: DecisionOwnerGameState = {
    sessionId: 'sess-1',
    isGameActive: true,
    isPracticeContinuation: false,
    playerColor: 'white',
    moveHistory: [],
    ...stateOverrides,
  }
  const callbacks: DecisionOwnerCallbacks = {
    appendMoveMessage: vi.fn(),
    setBlunderAlert: vi.fn(),
    setShowFlash: vi.fn(),
    setResolvedReview: vi.fn(),
    onSrsFail: vi.fn(),
    playBuzzer: vi.fn(),
    playBlunderAudio: vi.fn(),
  }
  const owner = new DecisionOwner({ getGameState: () => gameState })
  // Default: hold a UI lease so the existing transient-UI assertions apply. Tests
  // that exercise the no-lease (unmounted) path call release() first.
  const release = owner.registerUICallbacks(callbacks)
  return { owner, callbacks, gameState, release }
}

// Outcome builders (generation 0 — matches the owner's seeded default).
const scheduled = (
  moveIndex: number,
  requestId: string,
  previousRequestId?: string,
): AnalysisOutcome => ({
  seq: seq++,
  generation: 0,
  sessionId: 'sess-1',
  moveIndex,
  requestId,
  status: 'scheduled',
  ...(previousRequestId ? { previousRequestId } : {}),
})

const resolved = (
  moveIndex: number,
  requestId: string,
  result: AnalysisResult,
): AnalysisOutcome => ({
  seq: seq++,
  generation: 0,
  sessionId: 'sess-1',
  moveIndex,
  requestId,
  status: 'resolved',
  result: { ...result, id: requestId, moveIndex },
})

const failed = (moveIndex: number, requestId: string): AnalysisOutcome => ({
  seq: seq++,
  generation: 0,
  sessionId: 'sess-1',
  moveIndex,
  requestId,
  status: 'failed',
})

const skipped = (moveIndex: number, requestId: string): AnalysisOutcome => ({
  seq: seq++,
  generation: 0,
  sessionId: 'sess-1',
  moveIndex,
  requestId,
  status: 'skipped',
})

/** A resolved outcome whose result is a recordable blunder at moveIndex. */
const blunderResult = (moveIndex: number): AnalysisResult =>
  makeResult({
    move: 'e2e4',
    bestMove: 'd2d4',
    bestEval: 50,
    playedEval: -250,
    delta: 300,
    classification: 'blunder',
    blunder: true,
    recordable: true,
    moveIndex,
  })

/** Feed benign resolved outcomes for [0, upto) so the frontier advances. */
const seedResolvedThrough = (owner: DecisionOwner, upto: number) => {
  for (let i = 0; i < upto; i++) {
    owner.handleOutcome(resolved(i, `seed-${i}`, makeResult({ moveIndex: i })))
  }
}

beforeEach(() => {
  seq = 0
  recordBlunderMock.mockReset()
  reviewSrsBlunderMock.mockReset()
  recordBlunderMock.mockResolvedValue({})
  reviewSrsBlunderMock.mockResolvedValue({})
})

// ---------------------------------------------------------------------------
// Frontier non-resolved states (Finding 1)
// ---------------------------------------------------------------------------

describe('DecisionOwner — frontier provisional states', () => {
  it('failed at moveIndex blocks advancement until reschedule or 30s abandon', () => {
    vi.useFakeTimers()
    try {
      const { owner } = makeOwner()
      seedResolvedThrough(owner, 2)
      expect(owner.nextIndex).toBe(2)

      owner.handleOutcome(failed(2, 'reqF'))
      expect(owner.frontierStatusAt(2)).toBe('failed_provisional')
      // A later index cannot advance past the provisional slot.
      owner.handleOutcome(resolved(3, 'req3', makeResult({ moveIndex: 3 })))
      expect(owner.nextIndex).toBe(2)

      vi.advanceTimersByTime(30_000)
      expect(owner.frontierStatusAt(2)).toBe('abandoned')
      expect(owner.hasAbandonedRequest('reqF')).toBe(true)
      // Now the boundary sweeps through 2 (abandoned) and 3 (resolved).
      expect(owner.nextIndex).toBe(4)
    } finally {
      vi.useRealTimers()
    }
  })

  it('skipped blocks (not pass-through); 30s no reschedule abandons and advances', () => {
    vi.useFakeTimers()
    try {
      const { owner } = makeOwner()
      seedResolvedThrough(owner, 2)
      owner.handleOutcome(skipped(2, 'reqS'))
      expect(owner.frontierStatusAt(2)).toBe('skipped_provisional')
      expect(owner.nextIndex).toBe(2)

      vi.advanceTimersByTime(30_000)
      expect(owner.frontierStatusAt(2)).toBe('abandoned')
      expect(owner.nextIndex).toBe(3)
    } finally {
      vi.useRealTimers()
    }
  })

  it('skipped → scheduled(reschedule) → resolved records (boundary never advanced past 2)', () => {
    const { owner } = makeOwner()
    seedResolvedThrough(owner, 2)
    owner.handleOutcome(skipped(2, 'reqA'))
    expect(owner.frontierStatusAt(2)).toBe('skipped_provisional')

    owner.registerBlunderContext('reqB', makeContext(2))
    owner.handleOutcome(scheduled(2, 'reqB', 'reqA'))
    expect(owner.frontierStatusAt(2)).toBe('pending_analysis')

    owner.handleOutcome(resolved(2, 'reqB', blunderResult(2)))
    expect(recordBlunderMock).toHaveBeenCalledTimes(1)
  })

  it('scheduled opens a pending_analysis slot that blocks until resolved', () => {
    const { owner } = makeOwner()
    seedResolvedThrough(owner, 2)
    owner.handleOutcome(scheduled(2, 'reqA'))
    expect(owner.frontierStatusAt(2)).toBe('pending_analysis')
    owner.handleOutcome(resolved(3, 'req3', makeResult({ moveIndex: 3 })))
    expect(owner.nextIndex).toBe(2)
  })
})

// ---------------------------------------------------------------------------
// Reschedule migration (Finding 1)
// ---------------------------------------------------------------------------

describe('DecisionOwner — reschedule migration', () => {
  it('migrates contextMap old→new so a recording is not lost', () => {
    const { owner } = makeOwner()
    seedResolvedThrough(owner, 2)
    owner.registerBlunderContext('reqA', makeContext(2))
    owner.handleOutcome(failed(2, 'reqA'))
    owner.handleOutcome(scheduled(2, 'reqB', 'reqA'))
    owner.handleOutcome(resolved(2, 'reqB', blunderResult(2)))
    expect(recordBlunderMock).toHaveBeenCalledTimes(1)
  })

  it('SRS slot identity updates + correct tombstone across reschedule', () => {
    vi.useFakeTimers()
    try {
      const { owner } = makeOwner()
      const review = makeSrsReview(2, { srsDecisionId: 'S' })
      owner.registerSrsReview('reqA', review)
      expect(owner.findSrsSlotBySrsDecisionId('S')?.status).toBe('awaiting_analysis')

      owner.handleOutcome(skipped(2, 'reqA'))
      expect(owner.findSrsSlotBySrsDecisionId('S')?.status).toBe('awaiting_retry')

      owner.handleOutcome(scheduled(2, 'reqB', 'reqA'))
      const slot = owner.findSrsSlotBySrsDecisionId('S')!
      expect(slot.requestId).toBe('reqB')
      expect(slot.status).toBe('awaiting_analysis')

      // Skip the live request again → fresh retry timer → 30s → terminal.
      owner.handleOutcome(skipped(2, 'reqB'))
      expect(owner.findSrsSlotBySrsDecisionId('S')?.status).toBe('awaiting_retry')
      vi.advanceTimersByTime(30_000)

      expect(owner.findSrsSlotBySrsDecisionId('S')?.status).toBe('terminal_error')
      expect(owner.findSrsSlotBySrsDecisionId('S')?.terminalError).toBe('retry_timeout')
      expect(owner.hasAbandonedRequest('reqB')).toBe(true)
      expect(owner.hasAbandonedRequest('reqA')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('tombstoned reschedule is ignored and suppresses a later resolved', () => {
    vi.useFakeTimers()
    try {
      const { owner } = makeOwner()
      seedResolvedThrough(owner, 2)
      owner.registerBlunderContext('reqA', makeContext(2))
      owner.handleOutcome(failed(2, 'reqA'))
      vi.advanceTimersByTime(30_000) // reqA abandoned
      expect(owner.hasAbandonedRequest('reqA')).toBe(true)

      // Late reschedule referencing the tombstoned predecessor.
      owner.handleOutcome(scheduled(2, 'reqB', 'reqA'))
      expect(owner.frontierStatusAt(2)).toBe('abandoned') // no new slot
      expect(owner.hasAbandonedRequest('reqB')).toBe(true)

      owner.handleOutcome(resolved(2, 'reqB', blunderResult(2)))
      expect(recordBlunderMock).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})

// ---------------------------------------------------------------------------
// SRS FIFO drain (Finding 2)
// ---------------------------------------------------------------------------

describe('DecisionOwner — SRS FIFO drain', () => {
  /** Register + resolve an SRS review so its slot reaches `pending`/in_flight. */
  const armAndResolveSrs = (
    owner: DecisionOwner,
    moveIndex: number,
    requestId: string,
    srsDecisionId: string,
  ) => {
    owner.registerSrsReview(requestId, makeSrsReview(moveIndex, { srsDecisionId }))
    owner.handleOutcome(
      resolved(moveIndex, requestId, makeResult({ moveIndex, delta: 300 })),
    )
  }

  it('a later registration blocks until the earlier one settles', async () => {
    let resolveFirst: (v: unknown) => void = () => {}
    reviewSrsBlunderMock.mockImplementationOnce(
      () => new Promise((res) => (resolveFirst = res)),
    )

    const { owner } = makeOwner()
    armAndResolveSrs(owner, 2, 'reqA', 'S1') // seq 0 → in_flight
    armAndResolveSrs(owner, 4, 'reqB', 'S2') // seq 1 → blocked

    expect(reviewSrsBlunderMock).toHaveBeenCalledTimes(1)
    expect(owner.findSrsSlotBySrsDecisionId('S2')?.status).toBe('pending')

    resolveFirst({})
    await Promise.resolve()
    await Promise.resolve()

    expect(owner.findSrsSlotBySrsDecisionId('S1')?.status).toBe('succeeded')
    expect(reviewSrsBlunderMock).toHaveBeenCalledTimes(2)
  })

  it('a terminal predecessor unblocks the successor', () => {
    vi.useFakeTimers()
    try {
      const { owner } = makeOwner()
      // First review skipped → never resolves → 30s terminal_error.
      owner.registerSrsReview('reqA', makeSrsReview(2, { srsDecisionId: 'S1' }))
      owner.handleOutcome(skipped(2, 'reqA'))
      // Second review same blunderId resolves to a pending POST.
      owner.registerSrsReview('reqB', makeSrsReview(4, { srsDecisionId: 'S2' }))
      owner.handleOutcome(resolved(4, 'reqB', makeResult({ moveIndex: 4, delta: 300 })))

      // Blocked while S1 is awaiting_retry.
      expect(reviewSrsBlunderMock).not.toHaveBeenCalled()

      vi.advanceTimersByTime(30_000) // S1 → terminal_error
      expect(owner.findSrsSlotBySrsDecisionId('S1')?.status).toBe('terminal_error')
      expect(reviewSrsBlunderMock).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('registrationSeqCounter does not reset on full reset', () => {
    const { owner } = makeOwner()
    owner.registerSrsReview('reqA', makeSrsReview(2, { srsDecisionId: 'S1' }))
    const before = owner.registrationSeq
    owner.handleReset({ generation: 1, sessionId: 'sess-2' })
    owner.registerSrsReview('reqB', makeSrsReview(2, { srsDecisionId: 'S2' }))
    expect(owner.registrationSeq).toBeGreaterThan(before)
  })
})

// ---------------------------------------------------------------------------
// SRS eval_delta cap (g-no51)
// ---------------------------------------------------------------------------

describe('DecisionOwner — SRS eval_delta cap', () => {
  it('caps a mate-magnitude delta at EVAL_LOSS_CAP_CP (1000) in the review send', () => {
    const { owner } = makeOwner()
    owner.registerSrsReview('reqA', makeSrsReview(2, { srsDecisionId: 'S1' }))
    // A mate-magnitude delta of 10000 must reach the SRS API capped, not raw.
    owner.handleOutcome(
      resolved(2, 'reqA', makeResult({ moveIndex: 2, delta: 10000 })),
    )

    expect(reviewSrsBlunderMock).toHaveBeenCalledTimes(1)
    // Positional call: (session_id, blunder_id, passed, user_move, eval_delta, idempotency_key)
    expect(reviewSrsBlunderMock.mock.calls[0][4]).toBe(1000)
  })
})

// ---------------------------------------------------------------------------
// Registration after termination (Finding 3)
// ---------------------------------------------------------------------------

describe('DecisionOwner — registration after termination', () => {
  it('skipped then registerSrsReview opens directly in awaiting_retry', () => {
    const { owner } = makeOwner()
    seedResolvedThrough(owner, 2)
    owner.handleOutcome(skipped(2, 'reqA'))
    owner.registerSrsReview('reqA', makeSrsReview(2, { srsDecisionId: 'S' }))
    expect(owner.findSrsSlotBySrsDecisionId('S')?.status).toBe('awaiting_retry')
  })
})

// ---------------------------------------------------------------------------
// Blunder idempotency key (Finding 3)
// ---------------------------------------------------------------------------

describe('DecisionOwner — blunder idempotency key', () => {
  it('records with an idempotency_key and reuses the SAME key on retry', async () => {
    vi.useFakeTimers()
    try {
      recordBlunderMock
        .mockRejectedValueOnce(new ApiError('boom', { status: 500 }))
        .mockResolvedValueOnce({})

      const { owner } = makeOwner()
      seedResolvedThrough(owner, 2)
      owner.registerBlunderContext('reqC', makeContext(2))
      owner.handleOutcome(resolved(2, 'reqC', blunderResult(2)))

      await Promise.resolve()
      expect(recordBlunderMock).toHaveBeenCalledTimes(1)
      const firstKey = recordBlunderMock.mock.calls[0][7]
      expect(firstKey).toBeTruthy()

      await vi.advanceTimersByTimeAsync(2_000) // backoff 2^1 s
      expect(recordBlunderMock).toHaveBeenCalledTimes(2)
      expect(recordBlunderMock.mock.calls[1][7]).toBe(firstKey)
    } finally {
      vi.useRealTimers()
    }
  })
})

// ---------------------------------------------------------------------------
// Non-SRS blunder alert (Finding 4)
// ---------------------------------------------------------------------------

describe('DecisionOwner — non-SRS blunder alert', () => {
  it('buffers + flushes an alert without firing setResolvedReview', async () => {
    const { owner, callbacks } = makeOwner({
      moveHistory: [
        { san: 'e4', fen: 'after-0', uci: 'e2e4' },
        { san: 'e5', fen: 'after-1', uci: 'e7e5' },
        { san: 'Nf3', fen: 'after-2', uci: 'g1f3' },
      ],
    })
    seedResolvedThrough(owner, 2)
    owner.handleOutcome(resolved(2, 'reqC', blunderResult(2)))

    await Promise.resolve() // flush microtask
    expect(callbacks.setBlunderAlert).toHaveBeenCalledTimes(1)
    expect(callbacks.setShowFlash).toHaveBeenCalledWith(true)
    expect(callbacks.playBlunderAudio).toHaveBeenCalledTimes(1)
    expect(callbacks.setResolvedReview).not.toHaveBeenCalled()
  })

  it('drops a buffered alert when the epoch is bumped before flush', async () => {
    const { owner, callbacks } = makeOwner()
    seedResolvedThrough(owner, 2)
    owner.handleOutcome(resolved(2, 'reqC', blunderResult(2)))
    owner.handleReset({ generation: 1, sessionId: 'sess-2' }) // bumps alertEpoch

    await Promise.resolve()
    expect(callbacks.setBlunderAlert).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// HTTP retry classification (Finding 5)
// ---------------------------------------------------------------------------

describe('DecisionOwner — HTTP retry classification', () => {
  const recordOneBlunder = (owner: DecisionOwner) => {
    seedResolvedThrough(owner, 2)
    owner.registerBlunderContext('reqC', makeContext(2))
    owner.handleOutcome(resolved(2, 'reqC', blunderResult(2)))
  }

  const blunderEntry = (owner: DecisionOwner) =>
    owner.outboxSnapshot().find((e) => e.kind === 'blunder')!

  it('network error (TypeError) → awaiting_http_retry', async () => {
    recordBlunderMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { owner } = makeOwner()
    recordOneBlunder(owner)
    await Promise.resolve()
    await Promise.resolve()
    expect(blunderEntry(owner).status).toBe('awaiting_http_retry')
  })

  it('explicit 429 is retryable (NOT terminal)', async () => {
    recordBlunderMock.mockRejectedValueOnce(new ApiError('rate', { status: 429 }))
    const { owner } = makeOwner()
    recordOneBlunder(owner)
    await Promise.resolve()
    await Promise.resolve()
    expect(blunderEntry(owner).status).toBe('awaiting_http_retry')
  })

  it('LEGACY_AMBIGUOUS 409 → succeeded', async () => {
    recordBlunderMock.mockRejectedValueOnce(
      new ApiError('dup', {
        status: 409,
        details: { error_code: 'LEGACY_AMBIGUOUS' },
      }),
    )
    const { owner } = makeOwner()
    recordOneBlunder(owner)
    await Promise.resolve()
    await Promise.resolve()
    expect(blunderEntry(owner).status).toBe('succeeded')
  })

  it('non-retryable 4xx (IDEMPOTENCY_CONFLICT) → terminal_error', async () => {
    recordBlunderMock.mockRejectedValueOnce(
      new ApiError('conflict', {
        status: 409,
        details: { error_code: 'IDEMPOTENCY_CONFLICT' },
      }),
    )
    const { owner } = makeOwner()
    recordOneBlunder(owner)
    await Promise.resolve()
    await Promise.resolve()
    expect(blunderEntry(owner).status).toBe('terminal_error')
  })

  it('honors Retry-After over the computed backoff', async () => {
    vi.useFakeTimers()
    try {
      recordBlunderMock
        .mockRejectedValueOnce(
          new ApiError('rate', { status: 429, retryAfterMs: 10_000 }),
        )
        .mockResolvedValueOnce({})
      const { owner } = makeOwner()
      recordOneBlunder(owner)
      await Promise.resolve()
      await Promise.resolve()

      // Backoff would be 2s; Retry-After demands 10s — no retry before then.
      await vi.advanceTimersByTimeAsync(2_000)
      expect(recordBlunderMock).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(8_000)
      expect(recordBlunderMock).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})

// ---------------------------------------------------------------------------
// Committed boundary + partial reset
// ---------------------------------------------------------------------------

describe('DecisionOwner — committed boundary & reset', () => {
  it('partial-reset replay does not duplicate a blunder past the boundary', () => {
    const { owner } = makeOwner()
    seedResolvedThrough(owner, 5)
    expect(owner.committedIndex).toBe(5)

    owner.handleReset({ generation: 0, sessionId: 'sess-1', fromMoveIndex: 3 })
    expect(owner.nextIndex).toBe(3)
    expect(owner.committedIndex).toBe(5) // monotonic

    // Replay indices 3 and 4 with recordable blunders — boundary blocks recording.
    owner.registerBlunderContext('rep3', makeContext(3))
    owner.handleOutcome(resolved(3, 'rep3', blunderResult(3)))
    owner.registerBlunderContext('rep4', makeContext(4))
    owner.handleOutcome(resolved(4, 'rep4', blunderResult(4)))
    expect(recordBlunderMock).not.toHaveBeenCalled()
  })

  it('partial reset prunes tombstones for moveIndex >= k', () => {
    vi.useFakeTimers()
    try {
      const { owner } = makeOwner()
      seedResolvedThrough(owner, 3)
      owner.handleOutcome(failed(3, 'reqF'))
      vi.advanceTimersByTime(30_000)
      expect(owner.hasAbandonedRequest('reqF')).toBe(true)

      owner.handleReset({ generation: 0, sessionId: 'sess-1', fromMoveIndex: 3 })
      expect(owner.hasAbandonedRequest('reqF')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('only one blunder records per session (blunderReserved)', () => {
    const { owner } = makeOwner()
    seedResolvedThrough(owner, 2)
    owner.registerBlunderContext('reqC', makeContext(2))
    owner.handleOutcome(resolved(2, 'reqC', blunderResult(2)))
    owner.registerBlunderContext('reqD', makeContext(3))
    owner.handleOutcome(resolved(3, 'reqD', blunderResult(3)))
    expect(recordBlunderMock).toHaveBeenCalledTimes(1)
  })
})

// ---------------------------------------------------------------------------
// Stale generation + dispose
// ---------------------------------------------------------------------------

describe('DecisionOwner — stale generation & dispose', () => {
  it('drops outcomes from a stale generation', () => {
    const { owner } = makeOwner()
    seedResolvedThrough(owner, 2)
    owner.registerBlunderContext('reqC', makeContext(2))
    const stale: AnalysisOutcome = {
      ...resolved(2, 'reqC', blunderResult(2)),
      generation: 99,
    }
    owner.handleOutcome(stale)
    expect(recordBlunderMock).not.toHaveBeenCalled()
  })

  it('ignores a POST rejection that arrives after dispose() (no new retry timer)', async () => {
    vi.useFakeTimers()
    try {
      let rejectPost: (e: unknown) => void = () => {}
      recordBlunderMock.mockImplementationOnce(
        () => new Promise((_res, rej) => (rejectPost = rej)),
      )
      const { owner } = makeOwner()
      seedResolvedThrough(owner, 2)
      owner.registerBlunderContext('reqC', makeContext(2))
      owner.handleOutcome(resolved(2, 'reqC', blunderResult(2)))
      expect(recordBlunderMock).toHaveBeenCalledTimes(1)

      owner.dispose()
      // A 5xx that would normally schedule a retry lands AFTER disposal.
      rejectPost(new ApiError('boom', { status: 500 }))
      await Promise.resolve()
      await Promise.resolve()

      // No retry timer was scheduled — advancing time produces no second POST.
      await vi.advanceTimersByTimeAsync(60_000)
      expect(recordBlunderMock).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a superseded slot timer does not reintroduce a tombstone (identity guard)', () => {
    vi.useFakeTimers()
    try {
      const { owner } = makeOwner()
      seedResolvedThrough(owner, 2)
      // Two provisional outcomes for the same index: the first slot's 30s timer
      // is left pending while the second slot replaces it at that index.
      owner.handleOutcome(failed(2, 'reqA'))
      owner.handleOutcome(failed(2, 'reqA2'))

      vi.advanceTimersByTime(30_000)
      // Only the slot still live at index 2 may abandon; the superseded timer
      // must NOT tombstone its (now detached) request.
      expect(owner.hasAbandonedRequest('reqA')).toBe(false)
      expect(owner.hasAbandonedRequest('reqA2')).toBe(true)
      expect(owner.frontierStatusAt(2)).toBe('abandoned')
    } finally {
      vi.useRealTimers()
    }
  })

  it('dispose cancels pending provisional timers without throwing', () => {
    vi.useFakeTimers()
    try {
      const { owner } = makeOwner()
      seedResolvedThrough(owner, 2)
      owner.handleOutcome(failed(2, 'reqF'))
      owner.dispose()
      vi.advanceTimersByTime(30_000)
      // Timer was cancelled — slot never abandoned.
      expect(owner.frontierStatusAt(2)).toBe('failed_provisional')
      expect(owner.hasAbandonedRequest('reqF')).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })
})

// ---------------------------------------------------------------------------
// UI lease (g-2m0p): durable path always runs; transient UI is lease-gated
// ---------------------------------------------------------------------------

describe('DecisionOwner — UI lease', () => {
  it('with NO lease, a resolved blunder still POSTs recordBlunder but fires no alert', async () => {
    const { owner, callbacks, release } = makeOwner({
      moveHistory: [
        { san: 'e4', fen: 'after-0', uci: 'e2e4' },
        { san: 'e5', fen: 'after-1', uci: 'e7e5' },
        { san: 'Nf3', fen: 'after-2', uci: 'g1f3' },
      ],
    })
    release() // unmount: drop the UI lease

    seedResolvedThrough(owner, 2)
    owner.registerBlunderContext('reqC', makeContext(2))
    owner.handleOutcome(resolved(2, 'reqC', blunderResult(2)))

    await Promise.resolve() // flush microtask
    // Durable POST still landed.
    expect(recordBlunderMock).toHaveBeenCalledTimes(1)
    // Transient UI suppressed while unmounted.
    expect(callbacks.setBlunderAlert).not.toHaveBeenCalled()
    expect(callbacks.setShowFlash).not.toHaveBeenCalled()
    expect(callbacks.playBlunderAudio).not.toHaveBeenCalled()
  })

  it('releasing the lease before a queued alert flush suppresses the flash/audio', async () => {
    const { owner, callbacks, release } = makeOwner()
    seedResolvedThrough(owner, 2)
    owner.handleOutcome(resolved(2, 'reqC', blunderResult(2))) // buffers + schedules
    release() // lease released before the microtask flush → epoch bump drops it

    await Promise.resolve()
    expect(callbacks.setBlunderAlert).not.toHaveBeenCalled()
    expect(callbacks.playBlunderAudio).not.toHaveBeenCalled()
  })

  it('hasPendingReview is true while armed, false once resolved or cancelled', () => {
    const { owner } = makeOwner()
    owner.registerSrsReview('reqA', makeSrsReview(0, { srsDecisionId: 'SA' }))
    expect(owner.hasPendingReview('reqA')).toBe(true)
    // Resolution consumes the pending entry.
    owner.handleOutcome(resolved(0, 'reqA', makeResult({ moveIndex: 0, delta: 300 })))
    expect(owner.hasPendingReview('reqA')).toBe(false)

    owner.registerSrsReview('reqB', makeSrsReview(1, { srsDecisionId: 'SB' }))
    expect(owner.hasPendingReview('reqB')).toBe(true)
    owner.cancelPendingSrsReviews()
    expect(owner.hasPendingReview('reqB')).toBe(false)
  })

  it('cancelPendingSrsReviews terminates awaiting slots >= k, spares durable resolved ones', () => {
    const { owner } = makeOwner()
    // Durable: a resolved SRS slot at moveIndex 5 (in_flight POST).
    owner.registerSrsReview('reqA', makeSrsReview(5, { srsDecisionId: 'SA', blunderId: 100 }))
    owner.handleOutcome(resolved(5, 'reqA', makeResult({ moveIndex: 5, delta: 300 })))
    const durable = owner.findSrsSlotBySrsDecisionId('SA')!.status
    expect(['pending', 'in_flight', 'succeeded']).toContain(durable)

    // Not-yet-resolved SRS slot at moveIndex 6.
    owner.registerSrsReview('reqB', makeSrsReview(6, { srsDecisionId: 'SB', blunderId: 200 }))
    expect(owner.findSrsSlotBySrsDecisionId('SB')?.status).toBe('awaiting_analysis')

    owner.cancelPendingSrsReviews(5)

    // The awaiting slot (moveIndex 6 >= 5) is terminated; the durable one is untouched.
    expect(owner.findSrsSlotBySrsDecisionId('SB')?.status).toBe('terminal_error')
    expect(owner.findSrsSlotBySrsDecisionId('SB')?.terminalError).toBe('pruned')
    expect(owner.findSrsSlotBySrsDecisionId('SA')?.status).toBe(durable)

    // reqB's later resolved posts nothing — its pendingSrsMap entry was pruned.
    reviewSrsBlunderMock.mockClear()
    owner.handleOutcome(resolved(6, 'reqB', makeResult({ moveIndex: 6, delta: 300 })))
    expect(reviewSrsBlunderMock).not.toHaveBeenCalled()
  })
})

afterEach(() => {
  vi.useRealTimers()
})
