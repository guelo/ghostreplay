/**
 * The browser device runner (g-two-search-grade §10.1 bullet 2, §10.4).
 *
 * Drives the REAL bundled `analysisWorker` with production-shaped messages and
 * derives every metric from what the worker already emits. It re-implements no
 * orchestration: the reset policy, the shared budget, the search sequence, the
 * deadline stop/grace and the heartbeat are all the worker's, which is the only
 * way the resulting timings describe the shipping product (§10.1).
 *
 * This is the only source of iPhone/Safari and Android/Chrome timing in the
 * parent bead, and the mobile kill gate (g-grade-kill-gate) runs on top of it.
 */

import type {
  BenchCohort,
  BenchMoveRecord,
  BenchRecord,
  BenchRunRecord,
  BenchSourceStamp,
} from '../benchRecord'
import {
  BENCH_SCHEMA_VERSION,
  zeroedDivergences,
  zeroedRejections,
} from '../benchRecord'
import { armOrderBalanced, planWarnings } from '../method'
import {
  DEFAULT_ARM,
  armUnavailableReason,
  buildAnalyzeMessage,
  enableBenchMode,
} from '../benchProtocol'
import type { BenchWorkerLike } from '../benchProtocol'
import {
  createTranscriptCollector,
  feedLog,
  finishMove,
  parseWorkerLogLine,
} from '../transcript'
import { summarize } from '../summarize'
import type { AnalysisWorkerResponse, AnalyzeMoveMessage } from '../../workers/analysisMessages'
import {
  BASELINE_DEPTH,
  MAX_DEVICE_DEPTH,
  sessionAnalysisDepth,
} from '../../workers/deviceAnalysisTier'
import {
  BROWSER_ENGINE_IDENTITY,
  BROWSER_ENGINE_RESOURCES,
} from '../../workers/browserEngineIdentity'
import type { BenchBlock } from './schedule'
import { planBlocks, plannedMeasurements, totalItems } from './schedule'
import { DEFAULT_THERMAL_PLIES, buildPositionSet } from './positions'
import type { BenchRunConfig } from './config'
import { configProblems } from './config'
import { describeEnvironment, detectBuildMode } from './environment'
import { describeSource } from './source'

/** A worker as this runner uses it. The real `Worker` satisfies it. */
export type RunnerWorker = BenchWorkerLike & { terminate: () => void }

export type { BenchRunConfig } from './config'

export type BenchProgress = {
  done: number
  total: number
  blockIndex: number
  blockCount: number
  positionId: string
  phase: 'booting' | 'cooling' | 'measuring' | 'done' | 'stopped'
  lastRecord: BenchMoveRecord | null
  /** Remaining cooldown when `phase` is `cooling`. */
  cooldownMs?: number
}

export type BenchRunDeps = {
  createWorker: () => RunnerWorker
  now?: () => number
  newId?: () => string
  /** Injected so tests do not have to wait out a real cooldown. */
  sleep?: (ms: number) => Promise<void>
  onRecord?: (record: BenchRecord) => void
  onProgress?: (progress: BenchProgress) => void
}

/**
 * A boot that never answers `ready` must fail the block rather than the run —
 * generous, because a cold WASM start on a weak phone is genuinely slow
 * (`ANALYSIS_BOOT_TIMEOUT_MS` in the app is 20s for the same reason).
 */
const DEFAULT_READY_TIMEOUT_MS = 60_000

/**
 * One analyze-move at depth 17 runs up to three sequential single-threaded WASM
 * searches. This bound exists only to stop a wedged engine from hanging the
 * whole run; it must sit far above any healthy mobile move, so a timeout row
 * always means something is wrong rather than "this phone is slow".
 */
const DEFAULT_MOVE_TIMEOUT_MS = 180_000

const defaultId = () =>
  typeof crypto !== 'undefined' && crypto.randomUUID
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2)

class BenchStoppedError extends Error {
  constructor() {
    super('bench run stopped')
    this.name = 'BenchStoppedError'
  }
}

type ItemOutcome = {
  record: BenchMoveRecord
  /** True when the worker must be rebuilt before the next item. */
  workerCompromised: boolean
  /**
   * Host clock at the `uci` that marks the worker rebuilding its OWN engine
   * mid-measurement, or null when it did not. The next row's engine is cold and
   * the rebuild's cost belongs to it.
   */
  engineRebuiltAtMs: number | null
}

/**
 * Latches every `ready` the worker posts AFTER its boot handshake.
 *
 * The worker posts `ready` again whenever it rebuilds its engine internally, and
 * that message can arrive while no measurement is listening — so it is latched by
 * a listener that outlives one measurement rather than awaited on demand.
 */
const watchWorkerReady = (worker: RunnerWorker, now: () => number) => {
  let latchedAtMs: number | null = null
  let notify: ((atMs: number) => void) | null = null

  const onMessage = (event: MessageEvent) => {
    if ((event.data as AnalysisWorkerResponse)?.type !== 'ready') return
    const atMs = now()
    if (notify) {
      const resolve = notify
      notify = null
      resolve(atMs)
    } else {
      latchedAtMs = atMs
    }
  }
  worker.addEventListener('message', onMessage)

  return {
    dispose: () => worker.removeEventListener('message', onMessage),
    /** Host clock at the next (or already latched) `ready`; null on timeout. */
    next: (timeoutMs: number): Promise<number | null> => {
      if (latchedAtMs !== null) {
        const atMs = latchedAtMs
        latchedAtMs = null
        return Promise.resolve(atMs)
      }
      return new Promise((resolve) => {
        const timer = setTimeout(() => {
          notify = null
          resolve(null)
        }, timeoutMs)
        notify = (atMs) => {
          clearTimeout(timer)
          resolve(atMs)
        }
      })
    },
  }
}

export type BenchRunHandle = {
  promise: Promise<BenchRecord[]>
  /** Stop after the in-flight measurement; the partial run still summarizes. */
  stop: () => void
}

export const runBench = (config: BenchRunConfig, deps: BenchRunDeps): BenchRunHandle => {
  const now = deps.now ?? (() => performance.now())
  const newId = deps.newId ?? defaultId
  const sleep =
    deps.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)))
  const moveTimeoutMs = config.moveTimeoutMs ?? DEFAULT_MOVE_TIMEOUT_MS
  const readyTimeoutMs = config.readyTimeoutMs ?? DEFAULT_READY_TIMEOUT_MS
  const blockCooldownMs = Math.max(0, config.blockCooldownMs ?? 0)

  let stopped = false
  const stop = () => {
    stopped = true
  }
  /**
   * Whether the SCHEDULE was actually abandoned — not merely whether `stop()` was
   * pressed. Stop during the final measurement still finishes the plan, and a
   * summary that called such a run partial would understate complete evidence.
   */
  let aborted = false

  const promise = (async (): Promise<BenchRecord[]> => {
    const runId = newId()
    const runStartMs = now()
    // Before anything is built or measured: an unusable value here would
    // otherwise apply as silence (a `NaN` cooldown neither sleeps nor warns) or
    // as a different run than the header claims (an unknown mode executes as
    // `sequence`), and the file would look method-valid either way.
    const problems = configProblems(config)
    if (problems.length > 0) {
      throw new Error(`invalid bench configuration: ${problems.join('; ')}`)
    }
    const requestedDepth = config.depth ?? sessionAnalysisDepth()
    const sessionDepth = sessionAnalysisDepth()
    const positionSet = buildPositionSet(
      config.positionSetId,
      config.thermalPlies ?? DEFAULT_THERMAL_PLIES,
    )
    const blocks = planBlocks({
      arms: config.arms,
      repeats: config.repeats,
      positions: positionSet.positions,
      mode: config.mode,
      warmup: config.warmup,
    })
    const build = detectBuildMode()
    const source: BenchSourceStamp = { ...describeSource(), ...config.source }
    const plannedItems = plannedMeasurements(blocks)
    const balanced = armOrderBalanced(config.arms.length, Math.max(1, Math.floor(config.repeats)))
    const methodWarnings = planWarnings({
      repeats: config.repeats,
      armCount: config.arms.length,
      blockCount: blocks.length,
      armOrderBalanced: balanced,
      blockCooldownMs,
      thermalPlies: positionSet.isThermalSequence ? positionSet.positions.length : null,
      build,
      requestedDepth,
      sessionDepth,
      source,
    })

    const records: BenchRecord[] = []
    const moveRecords: BenchMoveRecord[] = []
    const emit = (record: BenchRecord) => {
      records.push(record)
      deps.onRecord?.(record)
    }

    const runRecord: BenchRunRecord = {
      kind: 'run',
      schemaVersion: BENCH_SCHEMA_VERSION,
      runId,
      harness: 'device',
      build,
      startedAtIso: new Date().toISOString(),
      engine: {
        engineVersion: BROWSER_ENGINE_IDENTITY.engine_version,
        engineBuild: BROWSER_ENGINE_IDENTITY.engine_build,
        evalFileId: BROWSER_ENGINE_IDENTITY.eval_file_id,
        threads: BROWSER_ENGINE_RESOURCES.threads,
        hashMb: BROWSER_ENGINE_RESOURCES.hash_mb,
      },
      depth: {
        baseline: BASELINE_DEPTH,
        maxDevice: MAX_DEVICE_DEPTH,
        session: sessionDepth,
        requested: requestedDepth,
      },
      environment: describeEnvironment(),
      source,
      device: { label: config.deviceLabel, notes: config.notes },
      plan: {
        mode: config.mode,
        arms: config.arms,
        repeats: config.repeats,
        positionSetId: positionSet.id,
        positionCount: positionSet.positions.length,
        blockCount: blocks.length,
        plannedItems,
        warmup: Boolean(config.warmup),
        armOrderBalanced: balanced,
        blockCooldownMs,
      },
      methodWarnings,
      countersAre: 'observer-recomputed',
    }
    emit(runRecord)

    const total = totalItems(blocks)
    let done = 0
    let seq = 0

    const report = (
      block: BenchBlock,
      positionId: string,
      phase: BenchProgress['phase'],
      lastRecord: BenchMoveRecord | null,
      cooldownMs?: number,
    ) => {
      deps.onProgress?.({
        done,
        total,
        blockIndex: block.blockIndex,
        blockCount: blocks.length,
        positionId,
        phase,
        lastRecord,
        ...(cooldownMs === undefined ? {} : { cooldownMs }),
      })
    }

    try {
      for (const block of blocks) {
        if (stopped) throw new BenchStoppedError()

        if (blockCooldownMs > 0 && block.blockIndex > 0) {
          // Let the device shed the previous block's heat, so the next arm is not
          // measured on the temperature the last one left behind.
          report(block, '', 'cooling', null, blockCooldownMs)
          await sleep(blockCooldownMs)
          if (stopped) throw new BenchStoppedError()
        }

        report(block, block.items[0]?.position.positionId ?? '', 'booting', null)

        // A holder rather than a plain `let`: the assignment happens inside the
        // `boot` closure, which TypeScript's control-flow analysis cannot see.
        const workerRef: { current: RunnerWorker | null } = { current: null }
        const readyWatchRef: { current: ReturnType<typeof watchWorkerReady> | null } = {
          current: null,
        }
        let workerBootMs: number | null = null
        let bootError: string | null = null
        /**
         * Set whenever the engine is rebuilt PART-WAY through a block — by this
         * runner, or by the worker itself.
         *
         * The schedule labelled the following item `warm`, but its engine is a
         * brand-new WASM instance with an empty hash — genuinely cold. Left
         * unmarked it would sit in the warm cohort as a slow outlier and quietly
         * inflate the warm median, so the row is relabelled and flagged.
         */
        let restartedMidBlock = false
        /** That rebuild's boot cost, which belongs to the row it precedes. */
        let restartBootMs: number | null = null

        const boot = async () => {
          readyWatchRef.current?.dispose()
          readyWatchRef.current = null
          workerRef.current?.terminate()
          const bootStart = now()
          const next = deps.createWorker()
          workerRef.current = next
          try {
            await waitForReady(next, readyTimeoutMs)
            workerBootMs = now() - bootStart
            bootError = null
          } catch (error) {
            bootError = error instanceof Error ? error.message : String(error)
            workerBootMs = null
          }
          // Installed only now, so the boot handshake's own `ready` is already
          // consumed and any LATER one can only mean an internal engine rebuild.
          readyWatchRef.current = watchWorkerReady(next, now)
          // Key 1 of the §15.1 C7 two-key opt-in, and only for a candidate arm:
          // the default arm must post exactly what production posts, so it never
          // sends `bench-init`. A build without the worker-side half never
          // answers, which resolves to no arms — and the candidate is then
          // refused here instead of being silently measured as `current`.
          if (block.arm !== DEFAULT_ARM) {
            const arms = await enableBenchMode(next, config.benchHandshakeTimeoutMs)
            const unavailable = armUnavailableReason(block.arm, arms)
            if (unavailable) {
              throw new Error(unavailable)
            }
          }
        }

        try {
          await boot()

          for (const item of block.items) {
            if (stopped) throw new BenchStoppedError()
            report(block, item.position.positionId, 'measuring', null)

            // A rebuilt engine is a cold engine whatever the schedule said, and it
            // carries the boot cost of that rebuild.
            const restarted = restartedMidBlock
            restartedMidBlock = false
            const cohort = restarted ? 'cold' : item.cohort
            const bootMs = restarted ? restartBootMs : item.itemIndex === 0 ? workerBootMs : null
            restartBootMs = null

            if (bootError) {
              // The worker never became ready: record the item as failed rather
              // than posting into a dead worker and waiting out the move timeout
              // for every remaining position.
              const record = emptyMoveRecord({
                runId,
                seq: seq++,
                block,
                item,
                cohort,
                workerRestarted: restarted,
                requestedDepth,
                workerBootMs: bootMs,
                runElapsedMs: now() - runStartMs,
                error: `worker boot failed: ${bootError}`,
              })
              moveRecords.push(record)
              emit(record)
              done += 1
              report(block, item.position.positionId, 'measuring', record)
              await boot()
              restartedMidBlock = true
              restartBootMs = workerBootMs
              continue
            }

            const outcome = await measureItem({
              worker: workerRef.current!,
              runId,
              seq: seq++,
              block,
              item,
              cohort,
              workerRestarted: restarted,
              requestedDepth,
              workerBootMs: bootMs,
              runElapsedMs: now() - runStartMs,
              moveTimeoutMs,
              now,
              newId,
            })

            moveRecords.push(outcome.record)
            emit(outcome.record)
            done += 1
            report(block, item.position.positionId, 'measuring', outcome.record)

            if (outcome.workerCompromised) {
              // A timeout or an unscoped worker error leaves engine state unknown.
              // Rebuilding is the only way the NEXT row is still a clean
              // measurement; `restartedMidBlock` then moves that row into the cold
              // cohort where it belongs.
              await boot()
              restartedMidBlock = true
              restartBootMs = workerBootMs
            } else if (outcome.engineRebuiltAtMs !== null) {
              // The worker tore its own engine down mid-move and is already
              // rebuilding it, reporting only a request-scoped error. Wait for the
              // replacement to report ready — otherwise the next measurement's
              // latency quietly contains the rest of that rebuild — and hand the
              // next row the cold cohort and the rebuild's cost.
              const readyAtMs = (await readyWatchRef.current?.next(readyTimeoutMs)) ?? null
              if (readyAtMs === null) {
                // The replacement engine never reported ready, which is no better
                // than a wedged worker: rebuild it outright.
                await boot()
                restartBootMs = workerBootMs
              } else {
                restartBootMs = readyAtMs - outcome.engineRebuiltAtMs
              }
              restartedMidBlock = true
            }
          }
        } finally {
          readyWatchRef.current?.dispose()
          readyWatchRef.current = null
          workerRef.current?.terminate()
          workerRef.current = null
        }
      }
    } catch (error) {
      if (!(error instanceof BenchStoppedError)) {
        throw error
      }
      aborted = true
    }

    const summary = summarize(runId, moveRecords, {
      completion: aborted ? 'stopped' : 'complete',
      plannedItems,
      planWarnings: methodWarnings,
    })
    emit(summary)
    deps.onProgress?.({
      done,
      total,
      blockIndex: blocks.length - 1,
      blockCount: blocks.length,
      positionId: '',
      phase: aborted ? 'stopped' : 'done',
      lastRecord: moveRecords[moveRecords.length - 1] ?? null,
    })

    return records
  })()

  return { promise, stop }
}

const waitForReady = (worker: RunnerWorker, timeoutMs: number) =>
  new Promise<void>((resolve, reject) => {
    const onMessage = (event: MessageEvent) => {
      const data = event.data as AnalysisWorkerResponse
      if (data?.type === 'ready') {
        cleanup()
        resolve()
      } else if (data?.type === 'error' && !data.id) {
        cleanup()
        reject(new Error(data.error))
      }
    }
    const timer = setTimeout(() => {
      cleanup()
      reject(new Error(`worker did not report ready within ${timeoutMs}ms`))
    }, timeoutMs)
    const cleanup = () => {
      clearTimeout(timer)
      worker.removeEventListener('message', onMessage)
    }
    worker.addEventListener('message', onMessage)
  })

type MeasureArgs = {
  worker: RunnerWorker
  runId: string
  seq: number
  block: BenchBlock
  item: BenchBlock['items'][number]
  /** The effective cohort, which a mid-block restart can override to `cold`. */
  cohort: BenchCohort
  workerRestarted: boolean
  requestedDepth: number
  workerBootMs: number | null
  runElapsedMs: number
  moveTimeoutMs: number
  now: () => number
  newId: () => string
}

const emptyMoveRecord = (args: {
  runId: string
  seq: number
  block: BenchBlock
  item: BenchBlock['items'][number]
  cohort: BenchCohort
  workerRestarted: boolean
  requestedDepth: number
  workerBootMs: number | null
  runElapsedMs: number
  error: string
}): BenchMoveRecord => ({
  kind: 'move',
  schemaVersion: BENCH_SCHEMA_VERSION,
  runId: args.runId,
  seq: args.seq,
  blockIndex: args.block.blockIndex,
  repeat: args.block.repeat,
  arm: args.block.arm,
  orderIndex: args.block.orderIndex,
  positionId: args.item.position.positionId,
  fen: args.item.position.fen,
  playedMove: args.item.position.playedMove,
  playerColor: args.item.position.playerColor,
  thermalIndex: args.item.position.thermalIndex,
  cohort: args.cohort,
  warmup: args.item.warmup,
  workerRestarted: args.workerRestarted,
  // No analyze-move was posted, so there is no transcript to have seen one in.
  engineRebuilt: false,
  requestedDepth: args.requestedDepth,
  e2eMs: 0,
  runElapsedMs: args.runElapsedMs,
  workerBootMs: args.workerBootMs,
  resetMs: null,
  phases: [],
  searchCount: 0,
  totalNodes: null,
  totalEngineMs: null,
  result: null,
  pEqualsB: null,
  rejections: zeroedRejections(),
  legacySelectorDivergence: 0,
  divergenceByReason: zeroedDivergences(),
  progressPings: 0,
  streamingPings: 0,
  error: args.error,
})

/**
 * One measurement: post a production-shaped analyze-move and fold everything the
 * worker says back into one row.
 *
 * postMessage preserves order, so the whole UCI transcript for this move arrives
 * BEFORE the `analysis` response that ends the wait — a row is therefore never
 * assembled from a partial phase list.
 *
 * That same ordering is why the transcript is only BUFFERED while the clock runs
 * and analysed afterwards. Parsing a line costs tokenization, UCI parsing,
 * snapshot assembly and (for a short PV) a chess.js legality replay, all on the
 * main thread, and the `analysis` response queues behind every log message the
 * search emitted — so analysing in the handler would add the observer's own cost
 * to the endpoint it is timing, and add more of it to whichever arm emits more
 * info lines. On a two-core phone it also competes with Stockfish itself.
 */
const measureItem = async (args: MeasureArgs): Promise<ItemOutcome> => {
  const { worker, item, block, now } = args
  const id = args.newId()
  const collector = createTranscriptCollector()
  /** Raw log text plus its host-clock receipt time, replayed after the clock stops. */
  const transcript: Array<{ message: string; atMs: number }> = []
  let progressPings = 0
  let streamingPings = 0

  const request: AnalyzeMoveMessage = {
    type: 'analyze-move',
    id,
    fen: item.position.fen,
    move: item.position.playedMove,
    playerColor: item.position.playerColor,
    depth: args.requestedDepth,
  }

  const settled = new Promise<{
    analysis: Extract<AnalysisWorkerResponse, { type: 'analysis' }> | null
    error: string | null
    fatal: boolean
  }>((resolve) => {
    const onMessage = (event: MessageEvent) => {
      const data = event.data as AnalysisWorkerResponse
      if (!data) return
      switch (data.type) {
        case 'log':
          // Timestamp and store only — see the note on `measureItem`. `now()` is
          // the receipt time the replay below feeds back in, so phase wall clocks
          // and the reset window are unchanged by the deferral.
          transcript.push({ message: data.message, atMs: now() })
          return
        case 'analysis-progress':
          progressPings += 1
          return
        case 'analysis-streaming':
          streamingPings += 1
          return
        case 'analysis':
          if (data.id !== id) return
          cleanup()
          resolve({ analysis: data, error: null, fatal: false })
          return
        case 'error': {
          const scoped = Boolean(data.id)
          if (scoped && data.id !== id) return
          cleanup()
          // An unscoped error is an engine/bootstrap failure: the worker is not
          // trustworthy for the next row either.
          resolve({ analysis: null, error: data.error, fatal: !scoped })
          return
        }
        default:
          return
      }
    }
    const timer = setTimeout(() => {
      cleanup()
      resolve({
        analysis: null,
        error: `analyze-move timed out after ${args.moveTimeoutMs}ms`,
        fatal: true,
      })
    }, args.moveTimeoutMs)
    const cleanup = () => {
      clearTimeout(timer)
      worker.removeEventListener('message', onMessage)
    }
    worker.addEventListener('message', onMessage)
  })

  const startMs = now()
  worker.postMessage(buildAnalyzeMessage(request, block.arm))
  const outcome = await settled
  // One reading, used for both the latency and the open-phase wall clock, so the
  // replay below cannot leak into either.
  const endMs = now()
  const e2eMs = endMs - startMs

  // The clock has stopped: now do the observer's work, at the receipt times it
  // would have run at.
  for (const logged of transcript) {
    const entry = parseWorkerLogLine(logged.message)
    if (entry) feedLog(collector, entry, logged.atMs)
  }

  const { phases, resetMs } = finishMove(
    collector,
    { playedMove: item.position.playedMove, bestMove: outcome.analysis?.bestMove ?? null },
    endMs,
  )

  const rejections = zeroedRejections()
  const divergenceByReason = zeroedDivergences()
  let legacySelectorDivergence = 0
  for (const phase of phases) {
    // A phase with no snapshot never answered `bestmove`, so §4.2 acceptance is
    // undefined for it and it contributes to no counter. Counting it would invent
    // rejections the worker never made.
    if (phase.snapshot && !phase.snapshot.accepted) {
      rejections[phase.snapshot.reason] += 1
    }
    if (phase.legacyDivergence) {
      divergenceByReason[phase.legacyDivergence] += 1
      legacySelectorDivergence += 1
    }
  }

  const nodeValues = phases
    .map((phase) => phase.nodes)
    .filter((value): value is number => value !== null)
  const timeValues = phases
    .map((phase) => phase.timeMs)
    .filter((value): value is number => value !== null)

  const analysis = outcome.analysis
  // `uci` inside a measurement means the worker rebuilt its own engine (see
  // TranscriptCollector.engineBoots): the failure it reported is request-scoped,
  // but the engine behind the NEXT row is brand new.
  const engineRebuilt = collector.engineBoots > 0

  return {
    workerCompromised: outcome.fatal,
    engineRebuiltAtMs: engineRebuilt ? collector.engineBootStartMs : null,
    record: {
      kind: 'move',
      schemaVersion: BENCH_SCHEMA_VERSION,
      runId: args.runId,
      seq: args.seq,
      blockIndex: block.blockIndex,
      repeat: block.repeat,
      arm: block.arm,
      orderIndex: block.orderIndex,
      positionId: item.position.positionId,
      fen: item.position.fen,
      playedMove: item.position.playedMove,
      playerColor: item.position.playerColor,
      thermalIndex: item.position.thermalIndex,
      cohort: args.cohort,
      warmup: item.warmup,
      workerRestarted: args.workerRestarted,
      engineRebuilt,
      requestedDepth: args.requestedDepth,
      e2eMs,
      runElapsedMs: args.runElapsedMs,
      workerBootMs: args.workerBootMs,
      resetMs,
      phases,
      searchCount: phases.length,
      totalNodes: nodeValues.length > 0 ? nodeValues.reduce((a, b) => a + b, 0) : null,
      totalEngineMs: timeValues.length > 0 ? timeValues.reduce((a, b) => a + b, 0) : null,
      result: analysis
        ? {
            bestMove: analysis.bestMove,
            bestLine: analysis.bestLine,
            bestEval: analysis.bestEval,
            playedEval: analysis.playedEval,
            bestEvalMate: analysis.bestEvalMate,
            playedEvalMate: analysis.playedEvalMate,
            delta: analysis.delta,
            classification: analysis.classification,
            canonical: analysis.canonical,
            capFired: analysis.capFired,
            stopReason: analysis.stopReason,
            reachedDepth: analysis.reachedDepth,
          }
        : null,
      pEqualsB: analysis ? analysis.bestMove === item.position.playedMove : null,
      rejections,
      legacySelectorDivergence,
      divergenceByReason,
      progressPings,
      streamingPings,
      error: outcome.error,
    },
  }
}
