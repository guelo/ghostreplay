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
        bestEval: 10, playedEval: 10, currentPositionEval: 10,
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
          played_eval: 10, best_eval: 10, eval_delta: 0, classification: 'best',
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
      expect(uploadSessionMovesMock).toHaveBeenCalledWith('session-old', expect.any(Array))

      // Capture the payload that was sent
      const firstPayload = uploadSessionMovesMock.mock.calls[0][1]

      // Now switch to a new session with DIFFERENT move history
      coordinator.startSession('session-new')
      useGameStore.setState({ moveHistory: makeMoveHistory(5) })
      gameAnalysisStore.getState().resolveAnalysis(0, {
        id: 'new-a0', move: 'DIFFERENT', bestMove: 'DIFFERENT',
        bestEval: 99, playedEval: 99, currentPositionEval: 99,
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

  describe('latest request wins per move index', () => {
    it('ignores stale worker results for a replayed ply', () => {
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
          played_eval: 25,
          best_eval: 25,
          eval_delta: 0,
          classification: 'best',
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
          played_eval: 25,
          best_eval: 25,
          eval_delta: 0,
          classification: 'best',
        }],
      ]))
      await vi.advanceTimersByTimeAsync(0)

      expect(coordinator.store.getState().isAnalyzing).toBe(false)
      expect(coordinator.store.getState().analyzingMove).toBeNull()
      expect(coordinator.store.getState().streamingEval).toBeNull()
    })

    it('ignores incomplete cache hits and lets the worker finish the analysis', async () => {
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
          best_eval: null,
          eval_delta: null,
          classification: null,
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
          played_eval: 10, best_eval: 10, eval_delta: 0, classification: 'best',
        }],
        ['fen-1::uci-1', {
          move_san: 'm1', best_move_uci: 'uci-1', best_move_san: 'm1',
          played_eval: 5, best_eval: 5, eval_delta: 0, classification: 'best',
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
          played_eval: 3, best_eval: 3, eval_delta: 0, classification: 'best',
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
})
