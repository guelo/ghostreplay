import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useGameStore } from '../stores/useGameStore'
import { gameAnalysisStore } from '../stores/createAnalysisStore'
import type { MoveRecord } from '../components/chess-game/domain/movePresentation'

const lookupAnalysisCacheMock = vi.fn()
const uploadSessionMovesMock = vi.fn()

vi.mock('../utils/api', () => ({
  lookupAnalysisCache: (...args: unknown[]) => lookupAnalysisCacheMock(...args),
  uploadSessionMoves: (...args: unknown[]) => uploadSessionMovesMock(...args),
}))

// Stub Worker so the coordinator can instantiate without a real WASM runtime.
// Tests that exercise worker message handling call handleWorkerMessage directly.
class MockWorker {
  addEventListener = vi.fn()
  removeEventListener = vi.fn()
  postMessage = vi.fn()
  terminate = vi.fn()
}
vi.stubGlobal('Worker', MockWorker)

// Must import AFTER mocks are installed
const { GameAnalysisCoordinator } = await import('./GameAnalysisCoordinator')

const initialStoreState = useGameStore.getInitialState()

const makeMoveHistory = (count: number): MoveRecord[] =>
  Array.from({ length: count }, (_, i) => ({
    san: `m${i}`,
    fen: `fen-${i}`,
    uci: `uci-${i}`,
  }))

let coordinator: InstanceType<typeof GameAnalysisCoordinator>

beforeEach(() => {
  vi.useFakeTimers()
  useGameStore.setState({ ...initialStoreState }, true)
  gameAnalysisStore.getState().clearAll()
  lookupAnalysisCacheMock.mockReset()
  uploadSessionMovesMock.mockReset()
  // Default: cache misses, so the worker fallback is released. Tests that
  // exercise a trusted cache hit override this with `mockReturnValueOnce`.
  lookupAnalysisCacheMock.mockResolvedValue(new Map())
  coordinator = new GameAnalysisCoordinator()
})

afterEach(() => {
  coordinator.destroy()
  vi.useRealTimers()
})

describe('GameAnalysisCoordinator', () => {
  // ---------------------------------------------------------------
  // Issue #1: stale cache lookups must not leak into the new session
  // ---------------------------------------------------------------
  describe('session generation guard on cache lookups', () => {
    it('drops cache results that resolve after a session switch', async () => {
      coordinator.startSession('session-A')

      // Set up a deferred cache lookup promise we control
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      // Trigger an analysis which schedules a cache lookup
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)

      // Flush the debounced cache lookup timer
      vi.advanceTimersByTime(200)

      // Switch to a new session BEFORE the cache promise resolves
      coordinator.startSession('session-B')

      // Now resolve the old cache lookup
      const cacheResults = new Map([
        ['fen-0::e2e4', {
          move_san: 'e4',
          best_move_uci: 'e2e4',
          best_move_san: 'e4',
          played_eval: 25,
          best_eval: 25,
          eval_delta: 0,
          classification: 'best',
        }],
      ])
      resolveLookup(cacheResults)

      // Flush microtasks
      await vi.advanceTimersByTimeAsync(0)

      // Session-B's analysisMap should be empty — the stale result was dropped
      expect(coordinator.store.getState().analysisMap.size).toBe(0)
    })
  })

  // ---------------------------------------------------------------
  // Issue #2: retries must use frozen payload, not current state
  // ---------------------------------------------------------------
  describe('incremental upload retry uses frozen payload', () => {
    it('retries with the original payload after session switch', async () => {
      coordinator.startSession('session-old')

      // Populate state for session-old
      const oldHistory = makeMoveHistory(2)
      useGameStore.setState({ moveHistory: oldHistory })
      gameAnalysisStore.getState().resolveAnalysis(0, {
        id: 'a0', move: 'uci-0', bestMove: 'uci-0',
        bestEval: 10, playedEval: 10, currentPositionEval: 10, playedEvalMate: null, currentPositionEvalMate: null,
        moveIndex: 0, delta: 0, classification: 'best',
        blunder: false, recordable: false,
      })

      // Mark index 0 dirty and trigger flush
      // Access private uploadState via the coordinator's flushPendingUploads path:
      // Resolve an analysis result which marks dirty + may trigger flush
      // Instead, directly call flushPendingUploads after manually dirtying

      // Simulate: coordinator resolved analysis and marked dirty during gameplay.
      // We need to trigger the incremental timer.
      // The coordinator's resolveAnalysisResult marks dirty, but we can't call it
      // directly. Instead, let the interval timer fire.
      // First, let's populate the upload state by doing an analyzeMove that resolves.
      // Easier approach: use the interval timer + upload failure + retry.

      // Make the first upload fail so we get a retry
      uploadSessionMovesMock.mockRejectedValueOnce(new Error('network'))

      // Advance the incremental upload timer (3s)
      // But first we need dirty indices. Let's manually trigger via the
      // public flushPendingUploads which calls flushIncrementalUpload.
      // The coordinator tracks dirty indices internally. We can't access them
      // without going through analysis resolution. Let's use a simpler approach:
      // call analyzeMove, fake the worker response, then test retry.

      // Actually, let's just test the core logic more directly by verifying
      // the upload mock receives the correct sessionId and payload on retry.

      // Reset and take a different approach: use the incremental upload timer
      coordinator.startSession('session-old')
      useGameStore.setState({ moveHistory: makeMoveHistory(2) })

      // Resolve analysis for index 0 — this marks it dirty in uploadState
      // We need to go through the coordinator's resolution path. The simplest
      // way is to trigger it via a cache hit.
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      coordinator.analyzeMove('fen-0', 'uci-0', 'white', 0, 20)
      vi.advanceTimersByTime(200) // flush cache debounce

      resolveLookup(new Map([
        ['fen-0::uci-0', {
          move_san: 'm0', best_move_uci: 'uci-0', best_move_san: 'm0',
          best_line_uci: ['uci-0', 'reply-0'],
          played_eval: 10, best_eval: 10, eval_delta: 0, classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0) // resolve cache promise

      // index 0 should now be resolved and dirty
      expect(coordinator.store.getState().analysisMap.size).toBe(1)

      // Make upload fail
      uploadSessionMovesMock.mockRejectedValueOnce(new Error('network'))

      // Fire the 3-second incremental upload timer
      vi.advanceTimersByTime(3000)
      await vi.advanceTimersByTimeAsync(0) // flush upload promise rejection

      expect(uploadSessionMovesMock).toHaveBeenCalledTimes(1)
      expect(uploadSessionMovesMock).toHaveBeenCalledWith(
        'session-old',
        expect.any(Array),
        expect.objectContaining({ signal: expect.any(Object) }),
      )

      // Capture the payload that was sent
      const firstPayload = uploadSessionMovesMock.mock.calls[0][1]

      // Now switch to a new session with DIFFERENT move history
      coordinator.startSession('session-new')
      useGameStore.setState({ moveHistory: makeMoveHistory(5) })
      gameAnalysisStore.getState().resolveAnalysis(0, {
        id: 'new-a0', move: 'DIFFERENT', bestMove: 'DIFFERENT',
        bestEval: 99, playedEval: 99, currentPositionEval: 99, playedEvalMate: null, currentPositionEvalMate: null,
        moveIndex: 0, delta: 0, classification: 'best',
        blunder: false, recordable: false,
      })

      // The retry for session-old should fire with exponential backoff (1s)
      uploadSessionMovesMock.mockResolvedValueOnce({ moves_inserted: 1 })
      vi.advanceTimersByTime(1500)
      await vi.advanceTimersByTimeAsync(0)

      // The retry should have been sent with the SAME payload as the first attempt
      // and to the OLD session ID, not the new one.
      const retryCalls = uploadSessionMovesMock.mock.calls.filter(
        (c) => c[0] === 'session-old',
      )
      expect(retryCalls.length).toBe(2) // original + retry
      expect(retryCalls[1][1]).toEqual(firstPayload) // same frozen payload
    })
  })

  describe('drill upload response handling', () => {
    it('does not mutate drill state from upload response failure metadata', async () => {
      coordinator.startSession('session-drill')
      useGameStore.setState({
        sessionId: 'session-drill',
        drillState: 'active',
        drillTerminalReason: null,
        moveHistory: makeMoveHistory(1),
      })

      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      coordinator.analyzeMove('fen-0', 'uci-0', 'white', 0, 20)
      vi.advanceTimersByTime(200)
      resolveLookup(new Map([
        ['fen-0::uci-0', {
          move_san: 'm0', best_move_uci: 'best-0', best_move_san: 'b0',
          best_line_uci: ['best-0', 'reply-0'],
          played_eval: 10, best_eval: 50, eval_delta: 40, classification: 'mistake',
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)

      uploadSessionMovesMock.mockResolvedValueOnce({
        moves_inserted: 1,
        drill_state: 'failed',
        drill_terminal_reason: 'accuracy',
      })
      vi.advanceTimersByTime(3000)
      await vi.advanceTimersByTimeAsync(0)

      expect(useGameStore.getState().drillState).toBe('active')
      expect(useGameStore.getState().drillTerminalReason).toBeNull()
    })
  })

  describe('waitForAnalysis', () => {
    it('resolves immediately from cached analysis', async () => {
      coordinator.startSession('session-wait')
      coordinator.store.getState().resolveAnalysis(0, {
        id: 'cached',
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 10,
        playedEval: 10,
        currentPositionEval: 10,
        playedEvalMate: null,
        currentPositionEvalMate: null,
        moveIndex: 0,
        delta: 0,
        classification: 'best',
        blunder: false,
        recordable: false,
      })

      await expect(coordinator.waitForAnalysis(0)).resolves.toMatchObject({ id: 'cached' })
    })

    it('resolves when the pending worker result arrives', async () => {
      coordinator.startSession('session-wait')
      const requestId = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const pending = coordinator.waitForAnalysis(0)

      ;(coordinator as any).handleWorkerMessage({
        data: {
          type: 'analysis',
          id: requestId,
          move: 'e2e4',
          bestMove: 'e2e4',
          bestEval: 20,
          playedEval: 20,
          delta: 0,
          classification: 'best',
        },
      })

      // Worker result is buffered until the cache settles; flush the (missing)
      // cache lookup to release the buffered fallback.
      await vi.advanceTimersByTimeAsync(200)

      await expect(pending).resolves.toMatchObject({ id: requestId, move: 'e2e4' })
    })

    it('propagates a worker mate count into the resolved analysis', async () => {
      coordinator.startSession('session-mate')
      const requestId = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const pending = coordinator.waitForAnalysis(0)

      ;(coordinator as any).handleWorkerMessage({
        data: {
          type: 'analysis',
          id: requestId,
          move: 'e2e4',
          bestMove: 'e2e4',
          bestEval: 10000,
          playedEval: 10000,
          playedEvalMate: 2,
          bestEvalMate: 2,
          delta: 0,
          classification: 'best',
        },
      })

      await vi.advanceTimersByTimeAsync(200)

      await expect(pending).resolves.toMatchObject({
        playedEvalMate: 2,
        currentPositionEvalMate: 2,
      })
    })

    it('flips a cached white-relative mate count to player-relative for black', async () => {
      lookupAnalysisCacheMock.mockReturnValueOnce(
        Promise.resolve(new Map([
          ['fen-1::d7d5', {
            move_san: 'd5',
            best_move_uci: 'd7d5',
            best_move_san: 'd5',
            best_line_uci: ['d7d5', 'g1f3'],
            played_eval: -9980,
            played_eval_mate: -2,
            best_eval: -9980,
            eval_delta: 0,
            classification: 'best',
            position_trusted: true,
          move_trusted: true,
          }],
        ])),
      )

      coordinator.startSession('session-cached-mate')
      // Black move at ply 1.
      coordinator.analyzeMove('fen-1', 'd7d5', 'black', 1, 20)
      const pending = coordinator.waitForAnalysis(1)

      // Flush the debounced cache lookup + its resolution microtask.
      vi.advanceTimersByTime(200)
      await vi.advanceTimersByTimeAsync(0)

      await expect(pending).resolves.toMatchObject({
        playedEvalMate: 2,
        currentPositionEvalMate: 2,
      })
    })

    it('rejects when analysis is unavailable or the session changes', async () => {
      coordinator.startSession('session-wait')
      await expect(coordinator.waitForAnalysis(4)).rejects.toThrow(/not scheduled/i)

      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const pending = coordinator.waitForAnalysis(0)
      coordinator.startSession('session-next')

      await expect(pending).rejects.toThrow(/session changed/i)
    })
  })

  // ---------------------------------------------------------------
  // g-position-analysis Phase 6: drill-truth side channel
  // ---------------------------------------------------------------
  describe('waitForDrillGrade (drill-truth side channel)', () => {
    const sendWorker = (
      requestId: string | undefined,
      over: Record<string, unknown> = {},
    ) => {
      ;(coordinator as any).handleWorkerMessage({
        data: {
          type: 'analysis',
          id: requestId,
          move: 'e2e4',
          bestMove: 'e2e4',
          bestEval: 0,
          playedEval: 0,
          delta: 0,
          classification: 'good',
          ...over,
        },
      })
    }

    it('strictness-0 position-only hit: passes the exact best move without publishing', async () => {
      lookupAnalysisCacheMock.mockResolvedValueOnce(new Map([
        ['fen-0::c2c4', {
          move_san: null, best_move_uci: 'c2c4', best_move_san: 'c2c4',
          best_line_uci: null, best_eval: 25,
          played_eval: null, played_eval_mate: null, eval_delta: null,
          classification: null,
          position_trusted: true, move_trusted: false, position_eval_loss_cp: null,
        }],
      ]))
      coordinator.startSession('s')
      const outcomes: any[] = []
      coordinator.addAnalysisOutcomeListener((o) => outcomes.push(o))
      const requestId = coordinator.analyzeMove('fen-0', 'c2c4', 'white', 0, 20)
      const worker = (coordinator as any).worker as MockWorker
      worker.postMessage.mockClear()

      const grade = coordinator.waitForDrillGrade(0, 'c2c4', 0)
      vi.advanceTimersByTime(200)
      await vi.advanceTimersByTimeAsync(0)

      await expect(grade).resolves.toEqual({
        grade: 'pass', bestMove: 'c2c4', source: 'position',
      })
      // Pure side channel: no published result, no resolved outcome, worker not
      // cancelled, uploads never dirtied (analysisMap stays empty).
      expect(coordinator.store.getState().analysisMap.size).toBe(0)
      expect(outcomes.some((o) => o.status === 'resolved')).toBe(false)
      expect(worker.postMessage).not.toHaveBeenCalledWith({
        type: 'cancel-analysis', id: requestId,
      })
    })

    it('strictness-0 position-only hit: fails when the played move is not the best move', async () => {
      lookupAnalysisCacheMock.mockResolvedValueOnce(new Map([
        ['fen-0::g1f3', {
          best_move_uci: 'c2c4', position_trusted: true,
          move_trusted: false, position_eval_loss_cp: null,
        }],
      ]))
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'g1f3', 'white', 0, 20)

      const grade = coordinator.waitForDrillGrade(0, 'g1f3', 0)
      vi.advanceTimersByTime(200)
      await vi.advanceTimersByTimeAsync(0)

      await expect(grade).resolves.toEqual({
        grade: 'fail', bestMove: 'c2c4', source: 'position',
      })
    })

    it('strictness-0 with no trusted position: falls back to the worker (truth settles null, no hang)', async () => {
      coordinator.startSession('s') // default cache mock = miss
      const requestId = coordinator.analyzeMove('fen-0', 'g1f3', 'white', 0, 20)

      const grade = coordinator.waitForDrillGrade(0, 'g1f3', 0)
      // Cache misses -> drill truth settles null -> worker fallback awaits waitForAnalysis.
      await vi.advanceTimersByTimeAsync(200)
      sendWorker(requestId, { move: 'g1f3', bestMove: 'g1f3', delta: 0 })
      await vi.advanceTimersByTimeAsync(0)

      await expect(grade).resolves.toEqual({
        grade: 'pass', bestMove: 'g1f3', source: 'worker',
      })
    })

    it('strictness>0 grades from the backend loss WITHOUT awaiting the worker (finding #7)', async () => {
      // The published gate releases to the worker (no eval_delta), but drill truth
      // carries position_eval_loss_cp, so the drill grade resolves from cache.
      lookupAnalysisCacheMock.mockResolvedValueOnce(new Map([
        ['fen-0::e2e4', {
          best_move_uci: 'd2d4', best_move_san: 'd4', best_line_uci: ['d2d4', 'g8f6'],
          best_eval: 50, played_eval: 20, played_eval_mate: null,
          eval_delta: null, classification: 'good',
          position_trusted: true, move_trusted: true, position_eval_loss_cp: 30,
        }],
      ]))
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)

      const grade = coordinator.waitForDrillGrade(0, 'e2e4', 20)
      vi.advanceTimersByTime(200)
      await vi.advanceTimersByTimeAsync(0)

      // loss 30 > strictness 20 -> fail, from the position channel, no worker msg.
      await expect(grade).resolves.toEqual({
        grade: 'fail', bestMove: 'd2d4', source: 'position',
      })
      expect(coordinator.store.getState().analysisMap.size).toBe(0)
    })

    it('strictness>0 with null backend loss: falls back to the worker delta', async () => {
      lookupAnalysisCacheMock.mockResolvedValueOnce(new Map([
        ['fen-0::e2e4', {
          best_move_uci: 'd2d4', best_line_uci: ['d2d4', 'g8f6'], best_eval: 50,
          played_eval: 20, eval_delta: null, classification: 'good',
          position_trusted: true, move_trusted: true, position_eval_loss_cp: null,
        }],
      ]))
      coordinator.startSession('s')
      const requestId = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)

      const grade = coordinator.waitForDrillGrade(0, 'e2e4', 20)
      await vi.advanceTimersByTimeAsync(200) // cache settles, truth loss null -> worker fallback
      sendWorker(requestId, { move: 'e2e4', bestMove: 'e2e4', delta: 10 })
      await vi.advanceTimersByTimeAsync(0)

      // Worker delta 10 <= strictness 20 -> pass, from the worker channel.
      await expect(grade).resolves.toEqual({
        grade: 'pass', bestMove: 'e2e4', source: 'worker',
      })
    })

    it('fast settlement (finding #10): grades from settled drill truth called AFTER cache resolved', async () => {
      // Full-trust cache hit publishes AND records drill truth, tearing down
      // pending state. A waitForDrillGrade called afterwards must still grade.
      lookupAnalysisCacheMock.mockResolvedValueOnce(new Map([
        ['fen-0::e2e4', {
          move_san: 'e4', best_move_uci: 'd2d4', best_move_san: 'd4',
          best_line_uci: ['d2d4', 'g8f6'], best_eval: 50, played_eval: -150,
          eval_delta: 200, classification: 'blunder',
          position_trusted: true, move_trusted: true, position_eval_loss_cp: 30,
        }],
      ]))
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      vi.advanceTimersByTime(200)
      await vi.advanceTimersByTimeAsync(0)

      // Settled: published from cache, pending state torn down.
      expect(coordinator.store.getState().analysisMap.get(0)?.delta).toBe(200)

      const grade = await coordinator.waitForDrillGrade(0, 'e2e4', 20)
      expect(grade).toEqual({ grade: 'fail', bestMove: 'd2d4', source: 'position' })
    })

    it('fast settlement worker source: grades from analysisMap when truth settled null', async () => {
      coordinator.startSession('s') // cache miss
      const requestId = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      vi.advanceTimersByTime(200) // cache miss -> released, truth settles null
      sendWorker(requestId, { move: 'e2e4', bestMove: 'd2d4', delta: 80 })
      await vi.advanceTimersByTimeAsync(0)

      expect(coordinator.store.getState().analysisMap.has(0)).toBe(true)

      // Called AFTER settlement; truth record is null -> worker fallback reads the
      // settled analysisMap result, never rejecting "not pending".
      const grade = await coordinator.waitForDrillGrade(0, 'e2e4', 20)
      expect(grade).toEqual({ grade: 'fail', bestMove: 'd2d4', source: 'worker' })
    })

    it('full-trust hit publishes the snapshot delta while drill grades from the backend loss', async () => {
      // eval_delta (snapshot, 200) and position_eval_loss_cp (drill, 30) are
      // independent: the published result keeps the snapshot; the drill uses 30.
      lookupAnalysisCacheMock.mockResolvedValueOnce(new Map([
        ['fen-0::e2e4', {
          move_san: 'e4', best_move_uci: 'd2d4', best_move_san: 'd4',
          best_line_uci: ['d2d4', 'g8f6'], best_eval: 50, played_eval: -150,
          eval_delta: 200, classification: 'blunder',
          position_trusted: true, move_trusted: true, position_eval_loss_cp: 30,
        }],
      ]))
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)

      const grade = coordinator.waitForDrillGrade(0, 'e2e4', 20)
      vi.advanceTimersByTime(200)
      await vi.advanceTimersByTimeAsync(0)

      const published = coordinator.store.getState().analysisMap.get(0)
      expect(published?.delta).toBe(200)            // snapshot unchanged
      expect(published?.classification).toBe('blunder')
      expect(published?.bestMove).toBe('d2d4')      // honest position best, never `?? move`
      await expect(grade).resolves.toEqual({
        grade: 'fail', bestMove: 'd2d4', source: 'position', // graded from 30, not 200
      })
    })

    it('re-analyzing the same index rejects the awaiting drill-truth waiter', async () => {
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const grade = coordinator.waitForDrillGrade(0, 'e2e4', 0)
      // Supersede before the cache lookup dispatches.
      coordinator.analyzeMove('fen-0b', 'd2d4', 'white', 0, 20)
      await expect(grade).rejects.toThrow(/superseded/i)
    })

    it('pruneFromMoveIndex rejects and clears the awaiting drill-truth waiter', async () => {
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const grade = coordinator.waitForDrillGrade(0, 'e2e4', 0)
      coordinator.pruneFromMoveIndex(0)
      await expect(grade).rejects.toThrow(/reverted/i)
    })

    it('clearAnalysis drains the awaiting drill-truth waiter', async () => {
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const grade = coordinator.waitForDrillGrade(0, 'e2e4', 0)
      coordinator.clearAnalysis()
      await expect(grade).rejects.toThrow(/cleared/i)
    })

    it('a session change rejects the awaiting drill-truth waiter', async () => {
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const grade = coordinator.waitForDrillGrade(0, 'e2e4', 0)
      coordinator.startSession('s2')
      await expect(grade).rejects.toThrow(/session changed/i)
    })

    it('an unscheduled index resolves drill truth null and rejects via the worker fallback (no hang)', async () => {
      coordinator.startSession('s')
      // Index 9 was never analyzed: no record, no current request -> truth null ->
      // worker fallback delegates to waitForAnalysis, which rejects to recovery.
      await expect(coordinator.waitForDrillGrade(9, 'e2e4', 0)).rejects.toThrow(/not scheduled/i)
    })
  })

  // ---------------------------------------------------------------
  // Issue #3: startSession resets sticky error status
  // ---------------------------------------------------------------
  describe('startSession resets error status', () => {
    it('clears error status so analysis is not permanently disabled', () => {
      coordinator.startSession('session-A')

      // Simulate a worker error
      coordinator.store.getState().setStatus('error')
      coordinator.store.getState().setError('WASM init failed')

      // analyzeMove should bail out
      const id = coordinator.analyzeMove('fen', 'e2e4', 'white', 0)
      expect(id).toBeUndefined()

      // Start a new session — status should be reset
      coordinator.startSession('session-B')

      // Status should no longer be 'error'
      expect(coordinator.store.getState().status).not.toBe('error')
      expect(coordinator.store.getState().error).toBeNull()
    })
  })

  describe('restartAnalysisWorker', () => {
    it('clears worker error state so analysis can be scheduled again', () => {
      coordinator.startSession('session-A')
      coordinator.store.getState().setStatus('error')
      coordinator.store.getState().setError('WASM init failed')

      expect(coordinator.analyzeMove('fen', 'e2e4', 'white', 0)).toBeUndefined()

      coordinator.restartAnalysisWorker()
      const id = coordinator.analyzeMove('fen', 'e2e4', 'white', 0)

      expect(id).toBeTruthy()
      expect(coordinator.store.getState().status).toBe('booting')
      expect(coordinator.store.getState().error).toBeNull()
    })
  })

  describe('practice continuation upload gating', () => {
    it('stops uploading newly resolved moves after session uploads are disabled', async () => {
      coordinator.startSession('session-practice')
      useGameStore.setState({ moveHistory: makeMoveHistory(2) })

      coordinator.stopSessionUploads()

      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      coordinator.analyzeMove('fen-0', 'uci-0', 'white', 0, 20)
      vi.advanceTimersByTime(200)

      resolveLookup(new Map([
        ['fen-0::uci-0', {
          move_san: 'm0', best_move_uci: 'uci-0', best_move_san: 'm0',
          best_line_uci: ['uci-0', 'reply-0'],
          played_eval: 10, best_eval: 10, eval_delta: 0, classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)

      vi.advanceTimersByTime(5000)
      await vi.advanceTimersByTimeAsync(0)

      expect(uploadSessionMovesMock).not.toHaveBeenCalled()
    })

    it('aborts an in-flight upload when session uploads are disabled', async () => {
      coordinator.startSession('session-practice')
      useGameStore.setState({ moveHistory: makeMoveHistory(1) })

      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      coordinator.analyzeMove('fen-0', 'uci-0', 'white', 0, 20)
      vi.advanceTimersByTime(200)

      resolveLookup(new Map([
        ['fen-0::uci-0', {
          move_san: 'm0', best_move_uci: 'uci-0', best_move_san: 'm0',
          best_line_uci: ['uci-0', 'reply-0'],
          played_eval: 10, best_eval: 10, eval_delta: 0, classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)

      uploadSessionMovesMock.mockReturnValueOnce(new Promise(() => {}))
      vi.advanceTimersByTime(3000)
      await vi.advanceTimersByTimeAsync(0)

      expect(uploadSessionMovesMock).toHaveBeenCalledTimes(1)
      const signal = uploadSessionMovesMock.mock.calls[0][2]?.signal as AbortSignal
      expect(signal.aborted).toBe(false)

      coordinator.stopSessionUploads()

      expect(signal.aborted).toBe(true)
    })
  })

  describe('latest request wins per move index', () => {
    it('ignores stale worker results for a replayed ply', async () => {
      coordinator.startSession('session-A')

      const firstId = coordinator.analyzeMove('fen-old', 'e2e4', 'white', 0, 20)
      const secondId = coordinator.analyzeMove('fen-new', 'd2d4', 'white', 0, 20)

      expect(firstId).toBeTruthy()
      expect(secondId).toBeTruthy()

      ;(coordinator as any).handleWorkerMessage({
        data: {
          type: 'analysis',
          id: firstId,
          move: 'e2e4',
          bestMove: 'e2e4',
          bestEval: 15,
          playedEval: 15,
          delta: 0,
          classification: 'best',
        },
      })

      expect(coordinator.store.getState().analysisMap.size).toBe(0)

      ;(coordinator as any).handleWorkerMessage({
        data: {
          type: 'analysis',
          id: secondId,
          move: 'd2d4',
          bestMove: 'd2d4',
          bestEval: 20,
          playedEval: 20,
          delta: 0,
          classification: 'best',
        },
      })

      // Second worker result is buffered until the cache misses and releases it.
      await vi.advanceTimersByTimeAsync(200)

      expect(coordinator.store.getState().analysisMap.get(0)?.id).toBe(secondId)
      expect(coordinator.store.getState().analysisMap.get(0)?.move).toBe('d2d4')
    })

    it('cancels the older worker request when a move index is replayed', () => {
      coordinator.startSession('session-A')

      const firstId = coordinator.analyzeMove('fen-old', 'e2e4', 'white', 0, 20)
      const worker = (coordinator as any).worker as MockWorker
      worker.postMessage.mockClear()

      coordinator.analyzeMove('fen-new', 'd2d4', 'white', 0, 20)

      expect(worker.postMessage).toHaveBeenCalledWith({
        type: 'cancel-analysis',
        id: firstId,
      })
    })

    it('clears the previous analysisMap entry as soon as a replay starts for that ply', () => {
      coordinator.startSession('session-A')

      ;(coordinator.store.getState()).resolveAnalysis(0, {
        id: 'old-id',
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 10,
        playedEval: 10,
        currentPositionEval: 10,
        playedEvalMate: null,
        currentPositionEvalMate: null,
        moveIndex: 0,
        delta: 0,
        classification: 'best',
        blunder: false,
        recordable: false,
      })

      expect(coordinator.store.getState().analysisMap.has(0)).toBe(true)

      coordinator.analyzeMove('fen-new', 'd2d4', 'white', 0, 20)

      expect(coordinator.store.getState().analysisMap.has(0)).toBe(false)
    })
  })

  describe('cache hits cancel worker analysis', () => {
    it('stops the matching worker request after a cache hit resolves the move', async () => {
      coordinator.startSession('session-A')

      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      const requestId = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const worker = (coordinator as any).worker as MockWorker
      worker.postMessage.mockClear()

      vi.advanceTimersByTime(200)

      resolveLookup(new Map([
        ['fen-0::e2e4', {
          move_san: 'e4',
          best_move_uci: 'e2e4',
          best_move_san: 'e4',
          best_line_uci: ['e2e4', 'e7e5'],
          played_eval: 25,
          best_eval: 25,
          eval_delta: 0,
          classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)

      expect(worker.postMessage).toHaveBeenCalledWith({
        type: 'cancel-analysis',
        id: requestId,
      })
    })

    it('clears analyzing state when a cache hit resolves the active request', async () => {
      coordinator.startSession('session-A')

      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      const requestId = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      ;(coordinator as any).handleWorkerMessage({
        data: {
          type: 'analysis-started',
          id: requestId,
          move: 'e2e4',
        },
      })

      expect(coordinator.store.getState().isAnalyzing).toBe(true)
      expect(coordinator.store.getState().analyzingMove).toBe('e2e4')

      vi.advanceTimersByTime(200)

      resolveLookup(new Map([
        ['fen-0::e2e4', {
          move_san: 'e4',
          best_move_uci: 'e2e4',
          best_move_san: 'e4',
          best_line_uci: ['e2e4', 'e7e5'],
          played_eval: 25,
          best_eval: 25,
          eval_delta: 0,
          classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)

      expect(coordinator.store.getState().isAnalyzing).toBe(false)
      expect(coordinator.store.getState().analyzingMove).toBeNull()
      expect(coordinator.store.getState().streamingEval).toBeNull()
    })

    it('threads best_line_uci from a cache hit into the resolved analysis bestLine', async () => {
      coordinator.startSession('session-A')

      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      vi.advanceTimersByTime(200)

      resolveLookup(new Map([
        ['fen-0::e2e4', {
          move_san: 'e4',
          best_move_uci: 'e2e4',
          best_move_san: 'e4',
          best_line_uci: ['e2e4', 'e7e5', 'g1f3'],
          played_eval: 25,
          best_eval: 25,
          eval_delta: 0,
          classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)

      expect(coordinator.store.getState().analysisMap.get(0)).toEqual(
        expect.objectContaining({
          bestMove: 'e2e4',
          bestLine: ['e2e4', 'e7e5', 'g1f3'],
        }),
      )
    })

    it('ignores cache hits without a usable best line and lets the worker finish the analysis', async () => {
      coordinator.startSession('session-A')

      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      const requestId = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const worker = (coordinator as any).worker as MockWorker
      worker.postMessage.mockClear()

      vi.advanceTimersByTime(200)

      resolveLookup(new Map([
        ['fen-0::e2e4', {
          move_san: 'e4',
          best_move_uci: 'e2e4',
          best_move_san: 'e4',
          played_eval: 25,
          best_eval: 25,
          eval_delta: 0,
          classification: 'best',
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)

      expect(coordinator.store.getState().analysisMap.size).toBe(0)
      expect(worker.postMessage).not.toHaveBeenCalledWith({
        type: 'cancel-analysis',
        id: requestId,
      })

      ;(coordinator as any).handleWorkerMessage({
        data: {
          type: 'analysis',
          id: requestId,
          move: 'e2e4',
          bestMove: 'e2e4',
          bestLine: ['e2e4', 'e7e5'],
          bestEval: 25,
          playedEval: 25,
          delta: 0,
          classification: 'best',
        },
      })

      expect(coordinator.store.getState().analysisMap.get(0)).toEqual(
        expect.objectContaining({
          id: requestId,
          move: 'e2e4',
          delta: 0,
          classification: 'best',
        }),
      )
    })
  })

  // ---------------------------------------------------------------
  // In-flight upload handoff: dirty indices accumulated while an
  // upload is in flight must still be drained after session switch
  // ---------------------------------------------------------------
  describe('detached upload state drains remaining dirty indices', () => {
    it('flushes leftover dirty indices on success even below threshold', async () => {
      coordinator.startSession('session-drain')
      useGameStore.setState({ moveHistory: makeMoveHistory(4) })

      // Resolve two analyses via cache to mark indices 0 and 1 dirty
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      coordinator.analyzeMove('fen-0', 'uci-0', 'white', 0, 20)
      coordinator.analyzeMove('fen-1', 'uci-1', 'black', 1, 20)
      vi.advanceTimersByTime(200) // flush cache debounce

      resolveLookup(new Map([
        ['fen-0::uci-0', {
          move_san: 'm0', best_move_uci: 'uci-0', best_move_san: 'm0',
          best_line_uci: ['uci-0', 'reply-0'],
          played_eval: 10, best_eval: 10, eval_delta: 0, classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
        ['fen-1::uci-1', {
          move_san: 'm1', best_move_uci: 'uci-1', best_move_san: 'm1',
          best_line_uci: ['uci-1', 'reply-1'],
          played_eval: 5, best_eval: 5, eval_delta: 0, classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)
      expect(coordinator.store.getState().analysisMap.size).toBe(2)

      // Start an upload for index 0 and 1. Make it hang via a deferred promise.
      let resolveUpload!: () => void
      uploadSessionMovesMock.mockReturnValueOnce(
        new Promise<{ moves_inserted: number }>((resolve) => {
          resolveUpload = () => resolve({ moves_inserted: 2 })
        }),
      )

      // Fire the 3-second incremental upload timer
      vi.advanceTimersByTime(3000)
      expect(uploadSessionMovesMock).toHaveBeenCalledTimes(1)

      // While that upload is in flight, resolve index 2 (marks dirty)
      let resolveLookup2!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup2 = resolve }),
      )
      coordinator.analyzeMove('fen-2', 'uci-2', 'white', 2, 20)
      vi.advanceTimersByTime(200)
      resolveLookup2(new Map([
        ['fen-2::uci-2', {
          move_san: 'm2', best_move_uci: 'uci-2', best_move_san: 'm2',
          best_line_uci: ['uci-2', 'reply-2'],
          played_eval: 3, best_eval: 3, eval_delta: 0, classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)

      // NOW switch sessions while upload is still in flight.
      // Index 2 is dirty with only 1 index — below the threshold of 4.
      coordinator.startSession('session-new')

      // Resolve the old in-flight upload
      uploadSessionMovesMock.mockResolvedValueOnce({ moves_inserted: 1 })
      resolveUpload()
      await vi.advanceTimersByTimeAsync(0)

      // The detached success handler should have flushed index 2
      // even though dirtyIndices.size (1) < INCREMENTAL_UPLOAD_BATCH_THRESHOLD (4)
      expect(uploadSessionMovesMock).toHaveBeenCalledTimes(2)
      expect(uploadSessionMovesMock.mock.calls[1][0]).toBe('session-drain')
    })
  })

  // ---------------------------------------------------------------
  // clearSession bumps generation so stale cache lookups are dropped
  // ---------------------------------------------------------------
  describe('clearSession bumps session generation', () => {
    it('drops cache results that resolve after clearSession', async () => {
      coordinator.startSession('session-C')

      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      vi.advanceTimersByTime(200)

      // Clear the session (reset/abandon) before cache resolves
      coordinator.clearSession()

      resolveLookup(new Map([
        ['fen-0::e2e4', {
          move_san: 'e4', best_move_uci: 'e2e4', best_move_san: 'e4',
          best_line_uci: ['e2e4', 'e7e5'],
          played_eval: 25, best_eval: 25, eval_delta: 0, classification: 'best',
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)

      // Store should remain empty — stale result dropped
      expect(coordinator.store.getState().analysisMap.size).toBe(0)
    })

    it('terminates the worker so stale searches do not keep running after reset', () => {
      coordinator.startSession('session-C')

      const worker = (coordinator as any).worker as MockWorker
      coordinator.clearSession()

      expect(worker.terminate).toHaveBeenCalled()
      expect((coordinator as any).worker).toBeNull()
    })
  })

  // ---------------------------------------------------------------
  // Cache-first authoritative resolution (g-cache-first-resolve)
  // ---------------------------------------------------------------
  describe('cache-first authoritative resolution', () => {
    const trustedRow = (move: string, overrides: Record<string, unknown> = {}) => ({
      move_san: move,
      best_move_uci: move,
      best_move_san: move,
      best_line_uci: [move, 'zzzz'],
      played_eval: 25,
      best_eval: 25,
      eval_delta: 0,
      classification: 'best' as const,
      played_eval_mate: null,
      best_eval_mate: null,
      position_trusted: true,
      move_trusted: true,
      analysis_profile_id: 'linux-sf18-d24',
      ...overrides,
    })

    const postWorker = (id: string, overrides: Record<string, unknown> = {}) => {
      ;(coordinator as any).handleWorkerMessage({
        data: {
          type: 'analysis',
          id,
          move: 'e2e4',
          bestMove: 'worker-best',
          bestEval: 11,
          playedEval: 11,
          delta: 0,
          classification: 'best',
          ...overrides,
        },
      })
    }

    it('publishes only the trusted cache result when the worker finishes first (AC1)', async () => {
      coordinator.startSession('s')
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(new Promise((r) => { resolveLookup = r }))

      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      const worker = (coordinator as any).worker as MockWorker
      worker.postMessage.mockClear()

      // Worker finishes first — buffered, not published.
      postWorker(id)
      expect(coordinator.store.getState().analysisMap.has(0)).toBe(false)

      vi.advanceTimersByTime(200)
      resolveLookup(new Map([['fen-0::e2e4', trustedRow('e2e4')]]))
      await vi.advanceTimersByTimeAsync(0)

      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('e2e4')
      expect(worker.postMessage).toHaveBeenCalledWith({ type: 'cancel-analysis', id })
    })

    it('publishes the worker result when the cache row is non-authoritative (AC3)', async () => {
      coordinator.startSession('s')
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(new Promise((r) => { resolveLookup = r }))

      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      postWorker(id)

      vi.advanceTimersByTime(200)
      resolveLookup(new Map([['fen-0::e2e4', trustedRow('e2e4', { position_trusted: false, move_trusted: false })]]))
      await vi.advanceTimersByTimeAsync(0)

      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('worker-best')
    })

    // Phase 5 grain split: the cache row resolves the move only when ALL of
    // isTrustedPositionHit, isTrustedMoveHit, and hasCpEvalLoss hold. Each of the
    // three concerns failing alone must fall back to the worker.
    it('falls back to the worker when the position is trusted but the move is not (split case a)', async () => {
      coordinator.startSession('s')
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(new Promise((r) => { resolveLookup = r }))

      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      postWorker(id)

      vi.advanceTimersByTime(200)
      resolveLookup(new Map([['fen-0::e2e4', trustedRow('e2e4', { move_trusted: false })]]))
      await vi.advanceTimersByTimeAsync(0)

      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('worker-best')
    })

    it('falls back to the worker when the move is trusted but the position is not (split case b)', async () => {
      coordinator.startSession('s')
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(new Promise((r) => { resolveLookup = r }))

      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      postWorker(id)

      vi.advanceTimersByTime(200)
      resolveLookup(new Map([['fen-0::e2e4', trustedRow('e2e4', { position_trusted: false })]]))
      await vi.advanceTimersByTimeAsync(0)

      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('worker-best')
    })

    it('falls back to the worker for a move-trusted mate-only row with no CP delta (split case c)', async () => {
      coordinator.startSession('s')
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(new Promise((r) => { resolveLookup = r }))

      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      postWorker(id)

      vi.advanceTimersByTime(200)
      // Both grains trusted, but eval_delta is null (mate-only) so the
      // transitional hasCpEvalLoss gate keeps it on the worker until Phase 6.
      resolveLookup(new Map([['fen-0::e2e4', trustedRow('e2e4', {
        classification: 'blunder',
        played_eval: null,
        played_eval_mate: -2,
        eval_delta: null,
      })]]))
      await vi.advanceTimersByTimeAsync(0)

      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('worker-best')
    })

    it('publishes the worker result on a cache miss (AC2)', async () => {
      coordinator.startSession('s')
      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      postWorker(id)
      await vi.advanceTimersByTimeAsync(200)
      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('worker-best')
    })

    it('publishes the worker result when the lookup rejects (AC2)', async () => {
      coordinator.startSession('s')
      // Create the rejection lazily (at call time) so the `.catch` in
      // flushCacheLookups attaches synchronously and no unhandled rejection is
      // emitted before the debounced mock is invoked.
      lookupAnalysisCacheMock.mockImplementationOnce(() => Promise.reject(new Error('network')))
      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      postWorker(id)
      await vi.advanceTimersByTimeAsync(200)
      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('worker-best')
    })

    it('cache-first hit ignores a late worker result and keeps it indexed (AC4, G1)', async () => {
      coordinator.startSession('s')
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(new Promise((r) => { resolveLookup = r }))

      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      vi.advanceTimersByTime(200)
      resolveLookup(new Map([['fen-0::e2e4', trustedRow('e2e4')]]))
      await vi.advanceTimersByTimeAsync(0)
      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('e2e4')

      const lastBefore = coordinator.store.getState().lastAnalysis
      // Late worker result for the same (settled) request must NOT clobber.
      postWorker(id)
      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('e2e4')
      expect(coordinator.store.getState().lastAnalysis).toBe(lastBefore)
    })

    it('resolved delta is identical whether the worker or the cache wins (AC4 anchor)', async () => {
      // This is the real, deferred cache/worker race that the ChessGame AC4
      // drill matrix composes on top of: the drill grades the settled result's
      // delta, so that delta must not depend on which side won. The cache row
      // and the worker carry the SAME delta; the winners differ (bestMove
      // 'e2e4' vs 'worker-best') but the drill-relevant delta is stable.
      const D = 30

      // Case A: trusted cache hit present; the worker finishes FIRST (buffered),
      // then the deferred lookup resolves → the cache wins.
      coordinator.startSession('sA')
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(new Promise((r) => { resolveLookup = r }))
      const idA = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      postWorker(idA, { delta: D, bestEval: D, playedEval: 0 })
      vi.advanceTimersByTime(200)
      resolveLookup(new Map([[
        'fen-0::e2e4',
        trustedRow('e2e4', { eval_delta: D, best_eval: D, played_eval: 0, classification: 'mistake' }),
      ]]))
      await vi.advanceTimersByTimeAsync(0)
      const cacheWon = coordinator.store.getState().analysisMap.get(0)
      expect(cacheWon?.bestMove).toBe('e2e4')
      expect(cacheWon?.delta).toBe(D)

      // Case B: cache MISSES (default mock); the worker result wins.
      coordinator.startSession('sB')
      const idB = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      postWorker(idB, { delta: D, bestEval: D, playedEval: 0 })
      await vi.advanceTimersByTimeAsync(200)
      const workerWon = coordinator.store.getState().analysisMap.get(0)
      expect(workerWon?.bestMove).toBe('worker-best')

      // Different winner, identical drill input.
      expect(workerWon?.delta).toBe(D)
      expect(workerWon?.delta).toBe(cacheWon?.delta)
    })

    it('total-analysis deadline rejects a stalled worker without hanging (AC7, 10b)', async () => {
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const pending = coordinator.waitForAnalysis(0)
      const rejection = expect(pending).rejects.toThrow(/timed out/i)

      // Cache misses (releases, no buffered worker), worker never emits.
      await vi.advanceTimersByTimeAsync(200)
      await vi.advanceTimersByTimeAsync(30_000)
      await rejection
      expect((coordinator as any).resolutionState.size).toBe(0)
    })

    it('keeps a slow-but-finite worker analysis alive past the old 8s deadline', async () => {
      coordinator.startSession('s')
      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      const pending = coordinator.waitForAnalysis(0)

      await vi.advanceTimersByTimeAsync(200)
      await vi.advanceTimersByTimeAsync(8200)

      expect(coordinator.store.getState().analysisMap.has(0)).toBe(false)
      expect((coordinator as any).resolutionState.has(0)).toBe(true)

      postWorker(id)

      await expect(pending).resolves.toMatchObject({
        id,
        bestMove: 'worker-best',
      })
      expect(coordinator.store.getState().analysisMap.get(0)?.id).toBe(id)
    })

    it('timeout releases the worker; a late trusted hit is ignored (AC2, R3)', async () => {
      coordinator.startSession('s')
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(new Promise((r) => { resolveLookup = r }))

      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      vi.advanceTimersByTime(200) // dispatch lookup, start cache timer
      vi.advanceTimersByTime(2500) // cache-response timeout fires → released

      postWorker(id) // released → worker resolves immediately
      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('worker-best')

      // Late trusted hit must be ignored.
      resolveLookup(new Map([['fen-0::e2e4', trustedRow('e2e4')]]))
      await vi.advanceTimersByTimeAsync(0)
      expect(coordinator.store.getState().analysisMap.get(0)?.bestMove).toBe('worker-best')
    })

    it('recovers a worker error via a trusted cache hit (AC6a)', async () => {
      coordinator.startSession('s')
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(new Promise((r) => { resolveLookup = r }))

      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      const pending = coordinator.waitForAnalysis(0)

      // Scoped worker error while cache pending — does NOT set global error.
      ;(coordinator as any).handleWorkerMessage({ data: { type: 'error', id, error: 'boom' } })
      expect(coordinator.store.getState().status).not.toBe('error')

      vi.advanceTimersByTime(200)
      resolveLookup(new Map([['fen-0::e2e4', trustedRow('e2e4')]]))
      await vi.advanceTimersByTimeAsync(0)

      await expect(pending).resolves.toMatchObject({ bestMove: 'e2e4' })
    })

    it('cache-miss-first then scoped worker error fails immediately (AC6b, 10e)', async () => {
      coordinator.startSession('s')
      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      const pending = coordinator.waitForAnalysis(0)
      const rejection = expect(pending).rejects.toThrow(/boom/)

      await vi.advanceTimersByTimeAsync(200) // cache miss → released, no worker

      // Scoped worker error after release → fail immediately (not after 8s).
      ;(coordinator as any).handleWorkerMessage({ data: { type: 'error', id, error: 'boom' } })
      await rejection
      expect((coordinator as any).resolutionState.size).toBe(0)
    })

    it('an unscoped worker error sets global error and tears down state (AC; F1/G3)', async () => {
      coordinator.startSession('s')
      let resolveLookup!: (v: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(new Promise((r) => { resolveLookup = r }))
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      vi.advanceTimersByTime(200)

      ;(coordinator as any).handleWorkerMessage({ data: { type: 'error', error: 'fatal' } })
      expect(coordinator.store.getState().status).toBe('error')
      expect((coordinator as any).resolutionState.size).toBe(0)

      // Late cache hit after fatal teardown must not write the store.
      resolveLookup(new Map([['fen-0::e2e4', trustedRow('e2e4')]]))
      await vi.advanceTimersByTimeAsync(0)
      expect(coordinator.store.getState().analysisMap.has(0)).toBe(false)
    })

    it('superseded waiter rejects and only the new request resolves (AC4, G2/F2)', async () => {
      coordinator.startSession('s')
      const firstId = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!
      const stale = coordinator.waitForAnalysis(0)
      const staleRejection = expect(stale).rejects.toThrow(/superseded/i)

      // Replay the same index — supersedes the first request immediately.
      const secondId = coordinator.analyzeMove('fen-new', 'd2d4', 'white', 0, 20)!
      await staleRejection

      const fresh = coordinator.waitForAnalysis(0)
      postWorker(secondId, { move: 'd2d4', bestMove: 'd2d4' })
      await vi.advanceTimersByTimeAsync(200)
      await expect(fresh).resolves.toMatchObject({ id: secondId })
      // The stale worker result must not settle the new request.
      postWorker(firstId)
      expect(coordinator.store.getState().analysisMap.get(0)?.id).toBe(secondId)
    })

    it('clearAnalysis drops a late worker result rather than repopulating lastAnalysis (Finding 1)', async () => {
      coordinator.startSession('s')
      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)!

      // Clear analysis while the worker is still alive (soft reset).
      coordinator.clearAnalysis()
      expect(coordinator.store.getState().lastAnalysis).toBeNull()

      // A late worker result for the discarded indexed request must be dropped,
      // not routed into the ad-hoc setLastAnalysis path.
      postWorker(id)
      expect(coordinator.store.getState().lastAnalysis).toBeNull()
      expect(coordinator.store.getState().analysisMap.has(0)).toBe(false)
    })

    it('restartAnalysisWorker rejects unresolved waiters (AC; R4)', async () => {
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const pending = coordinator.waitForAnalysis(0)
      const rejection = expect(pending).rejects.toThrow(/restarted/i)
      coordinator.restartAnalysisWorker()
      await rejection
      expect((coordinator as any).resolutionState.size).toBe(0)
    })
  })

  // ---------------------------------------------------------------
  // g-hpw4: typed AnalysisOutcome channel
  // ---------------------------------------------------------------
  describe('AnalysisOutcome channel', () => {
    const collect = () => {
      const outcomes: any[] = []
      const unsub = coordinator.addAnalysisOutcomeListener((o) => outcomes.push(o))
      return { outcomes, unsub }
    }

    it('emits scheduled on analyzeMove and resolved on a trusted cache hit', async () => {
      coordinator.startSession('s')
      const { outcomes } = collect()

      lookupAnalysisCacheMock.mockResolvedValueOnce(
        new Map([
          ['fen-0::e2e4', {
            best_move_uci: 'd2d4', best_line_uci: ['d2d4', 'g8f6'], best_eval: 50,
            played_eval: -150, played_eval_mate: null, eval_delta: 200,
            classification: 'blunder', analysis_profile_id: 'p1',
            position_trusted: true,
          move_trusted: true,
          }],
        ]),
      )
      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      vi.advanceTimersByTime(200)
      await vi.advanceTimersByTimeAsync(0)

      const scheduled = outcomes.find((o) => o.status === 'scheduled')
      const resolved = outcomes.find((o) => o.status === 'resolved')
      expect(scheduled).toMatchObject({ moveIndex: 0, requestId: id, generation: expect.any(Number) })
      expect(resolved).toMatchObject({ moveIndex: 0, requestId: id, status: 'resolved' })
      expect(resolved.result.delta).toBe(200)
    })

    it('emits failed for each unresolved index on restart, preserving lineage', () => {
      coordinator.startSession('s')
      const id = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const { outcomes } = collect()
      coordinator.restartAnalysisWorker()
      const failed = outcomes.find((o) => o.status === 'failed')
      expect(failed).toMatchObject({ moveIndex: 0, requestId: id, status: 'failed' })
      // Lineage preserved across same-generation cleanup → next schedule carries it.
      const { outcomes: o2 } = collect()
      const id2 = coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const scheduled = o2.find((o) => o.status === 'scheduled')
      expect(scheduled).toMatchObject({ requestId: id2, previousRequestId: id })
    })

    it('markSkipped emits exactly one skipped outcome per requestId', () => {
      coordinator.startSession('s')
      const { outcomes } = collect()
      coordinator.markSkipped(3, 'synthetic-3')
      coordinator.markSkipped(3, 'synthetic-3')
      const skipped = outcomes.filter((o) => o.status === 'skipped')
      expect(skipped).toHaveLength(1)
      expect(skipped[0]).toMatchObject({ moveIndex: 3, requestId: 'synthetic-3' })
    })

    it('re-emits skipped for a replayed synthetic id after pruneFromMoveIndex (P2)', () => {
      coordinator.startSession('s')
      const { outcomes } = collect()
      // First skip of the deterministic synthetic id for index 2.
      coordinator.markSkipped(2, 'analysis-2-g1f3')
      // Revert prunes index 2 (and clears its skipped-dedup guard).
      coordinator.pruneFromMoveIndex(2)
      // Replaying the same move yields the same synthetic id — it must skip again.
      coordinator.markSkipped(2, 'analysis-2-g1f3')
      const skipped = outcomes.filter((o) => o.status === 'skipped')
      expect(skipped).toHaveLength(2)
    })

    it('re-emits skipped for a replayed synthetic id after skip -> scheduled -> prune (P2)', () => {
      coordinator.startSession('s')
      const { outcomes } = collect()
      // skip (synthetic) -> retry scheduled (real id overwrites lineage) -> prune.
      coordinator.markSkipped(2, 'analysis-2-g1f3')
      coordinator.analyzeMove('fen-2', 'g1f3', 'white', 2, 20)
      coordinator.pruneFromMoveIndex(2)
      // Replaying the original synthetic id must still skip (not be suppressed).
      coordinator.markSkipped(2, 'analysis-2-g1f3')
      const skipped = outcomes.filter((o) => o.status === 'skipped')
      expect(skipped).toHaveLength(2)
    })

    it('startSession emits a full reset (no fromMoveIndex) with the new epoch', () => {
      coordinator.startSession('s1')
      const resets: any[] = []
      coordinator.addAnalysisResetListener((i) => resets.push(i))
      const before = coordinator.getEpoch().generation
      coordinator.startSession('s2')
      expect(resets).toHaveLength(1)
      expect(resets[0].fromMoveIndex).toBeUndefined()
      expect(resets[0].generation).toBe(before + 1)
    })

    it('pruneFromMoveIndex tombstones pruned ids and emits a partial reset', () => {
      coordinator.startSession('s')
      coordinator.analyzeMove('fen-0', 'e2e4', 'white', 0, 20)
      const id1 = coordinator.analyzeMove('fen-1', 'e7e5', 'black', 1, 20)
      const resets: any[] = []
      coordinator.addAnalysisResetListener((i) => resets.push(i))

      coordinator.pruneFromMoveIndex(1)

      expect(resets).toEqual([expect.objectContaining({ fromMoveIndex: 1 })])
      // A queued worker result for the pruned id is dropped (tombstone), not
      // treated as non-indexed (no lastAnalysis mutation).
      ;(coordinator as any).handleWorkerMessage({
        data: {
          type: 'analysis', id: id1, move: 'e7e5', bestMove: 'd2d4',
          bestLine: null, bestEval: 0, playedEval: 0, playedEvalMate: null,
          delta: 0, classification: 'good',
        },
      })
      expect(coordinator.store.getState().lastAnalysis).toBeNull()
    })
  })
})
