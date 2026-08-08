import { beforeEach, describe, expect, it, vi } from 'vitest'
import { runBench } from './runner'
import type { RunnerWorker } from './runner'
import type {
  BenchMoveRecord,
  BenchRecord,
  BenchRunRecord,
  BenchSummaryRecord,
} from '../benchRecord'

/**
 * A pass-through spy on the observer's only entry point, so a test can ask WHEN
 * the transcript was analysed rather than only what it produced.
 */
const observer = vi.hoisted(() => ({ feedLogCalls: 0 }))
vi.mock('../transcript', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../transcript')>()
  return {
    ...actual,
    feedLog: (...args: Parameters<typeof actual.feedLog>) => {
      observer.feedLogCalls += 1
      return actual.feedLog(...args)
    },
  }
})

const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

beforeEach(() => {
  observer.feedLogCalls = 0
})

type FakeBehaviour = {
  /**
   * What the worker answers with for each analyze-move, in order.
   *
   * `open-search` logs a `go` and some info lines but never `bestmove` and never
   * answers — the shape a wedged or crashed engine leaves behind.
   * `engine-rebuild` is the worker's deadline-grace path: it tears its engine down
   * mid-search (a fresh `uci`), reports a REQUEST-SCOPED error, and reports `ready`
   * again once the replacement engine is up.
   */
  script?: Array<
    'analysis' | 'scoped-error' | 'silent' | 'unscoped-error' | 'open-search' | 'engine-rebuild'
  >
  ready?: boolean
  bestMove?: string
  /**
   * Shared analyze counter, so a factory that hands out a FRESH fake per
   * `createWorker` call (as the runner does after a compromised worker) keeps
   * walking the same script.
   */
  counter?: { n: number }
  /** Called immediately before the `analysis` response is posted. */
  onResponse?: () => void
}

/**
 * A scripted stand-in for the analysis worker: it answers `ready`, mirrors a
 * plausible UCI transcript through `log` messages, and posts an `analysis`
 * result. The real worker is exercised by the Playwright baseline run; this fake
 * exists to pin the runner's message contract and record assembly.
 */
const createFakeWorker = (behaviour: FakeBehaviour = {}) => {
  const listeners = new Set<(event: MessageEvent) => void>()
  const posted: Array<Record<string, unknown>> = []
  const counter = behaviour.counter ?? { n: 0 }
  let terminated = false
  let announcedReady = false

  const send = (data: unknown) => {
    for (const listener of [...listeners]) {
      listener({ data } as MessageEvent)
    }
  }
  const log = (message: string) => send({ type: 'log', message })

  const worker: RunnerWorker = {
    postMessage: (message) => {
      const data = message as Record<string, unknown>
      posted.push(data)
      if (data.type !== 'analyze-move') return

      const mode = behaviour.script?.[counter.n] ?? 'analysis'
      counter.n += 1
      if (mode === 'silent') return

      if (mode === 'open-search' || mode === 'engine-rebuild') {
        queueMicrotask(() => {
          if (terminated) return
          log('[analysisWorker ->] ucinewgame')
          log('[analysisWorker ->] isready')
          log('[analysisWorker <-] readyok')
          log(`[analysisWorker ->] position fen ${data.fen}`)
          log(`[analysisWorker ->] go depth ${data.depth}`)
          log('[analysisWorker <-] info depth 2 multipv 1 score cp 25 nodes 1000 nps 5000 time 200 pv e2e4 d7d5')
          if (mode !== 'engine-rebuild') return

          // analysisWorker's deadline grace: `stop` goes unanswered, so
          // destroyEngine() terminates Stockfish and ensureEngine() sends `uci`
          // to its replacement — all before the failure is reported, and reported
          // SCOPED to this request, which is what makes it invisible otherwise.
          log('[analysisWorker ->] stop')
          log('[analysisWorker ->] uci')
          send({
            type: 'error',
            id: data.id,
            error: 'Engine did not stop before the analysis deadline grace expired',
          })
          queueMicrotask(() => {
            if (!terminated) send({ type: 'ready' })
          })
        })
        return
      }

      queueMicrotask(() => {
        if (terminated) return
        if (mode === 'unscoped-error') {
          send({ type: 'error', error: 'engine died' })
          return
        }
        if (mode === 'scoped-error') {
          send({ type: 'error', id: data.id, error: 'analysis failed' })
          return
        }

        const bestMove = behaviour.bestMove ?? 'd2d4'
        const played = data.move as string
        log('[analysisWorker ->] ucinewgame')
        log('[analysisWorker ->] isready')
        log('[analysisWorker <-] readyok')
        log(`[analysisWorker ->] position fen ${data.fen}`)
        log(`[analysisWorker ->] go depth ${data.depth}`)
        log(
          `[analysisWorker <-] info depth ${data.depth} multipv 1 score cp 25 nodes 1000 nps 5000 time 200 pv ${bestMove} d7d5`,
        )
        log(`[analysisWorker <-] bestmove ${bestMove}`)
        send({ type: 'analysis-progress', id: data.id })
        log(`[analysisWorker ->] position fen ${data.fen} moves ${played}`)
        log(`[analysisWorker ->] go depth ${data.depth}`)
        log(
          `[analysisWorker <-] info depth ${data.depth} multipv 1 score cp 15 nodes 2000 nps 5000 time 400 pv e7e5 g1f3`,
        )
        send({ type: 'analysis-streaming', id: data.id, cp: 15, depth: 3 })
        log('[analysisWorker <-] bestmove e7e5')
        if (played !== bestMove) {
          log(`[analysisWorker ->] position fen ${data.fen} moves ${bestMove}`)
          log(`[analysisWorker ->] go depth ${data.depth}`)
          log(
            `[analysisWorker <-] info depth ${data.depth} multipv 1 score cp 30 nodes 3000 nps 5000 time 600 pv d7d5 c2c4`,
          )
          log('[analysisWorker <-] bestmove d7d5')
        }
        behaviour.onResponse?.()
        send({
          type: 'analysis',
          id: data.id,
          move: played,
          bestMove,
          bestLine: [bestMove, 'd7d5'],
          bestEval: 30,
          playedEval: 15,
          bestEvalMate: null,
          playedEvalMate: null,
          delta: 15,
          classification: 'good',
          canonical: true,
          capFired: false,
          stopReason: 'bestmove',
          reachedDepth: data.depth,
        })
      })
    },
    addEventListener: (_type, listener) => {
      listeners.add(listener)
      // ONCE, like the real worker: `ready` is posted when the engine finishes its
      // boot handshake, not to whoever subscribes later. A `ready` after that means
      // the worker rebuilt its engine, which the runner treats as such.
      if (behaviour.ready !== false && !announcedReady) {
        announcedReady = true
        queueMicrotask(() => {
          if (!terminated) listener({ data: { type: 'ready' } } as MessageEvent)
        })
      }
    },
    removeEventListener: (_type, listener) => {
      listeners.delete(listener)
    },
    terminate: () => {
      terminated = true
      listeners.clear()
    },
  }

  return { worker, posted, isTerminated: () => terminated }
}

const singlePositionConfig = {
  deviceLabel: 'test device',
  notes: '',
  mode: 'sequence' as const,
  positionSetId: 'thermal-40' as const,
  thermalPlies: 1,
  repeats: 1,
  depth: 3,
  moveTimeoutMs: 200,
  readyTimeoutMs: 200,
}

const moveRows = (records: readonly BenchRecord[]): BenchMoveRecord[] =>
  records.filter((record): record is BenchMoveRecord => record.kind === 'move')

describe('runBench message contract', () => {
  it('posts exactly the production analyze-move shape', async () => {
    const fake = createFakeWorker()
    const { promise } = runBench(singlePositionConfig, { createWorker: () => fake.worker })
    await promise

    const analyze = fake.posted.filter((message) => message.type === 'analyze-move')
    expect(analyze).toHaveLength(1)
    expect(Object.keys(analyze[0]).sort()).toEqual(
      ['depth', 'fen', 'id', 'move', 'playerColor', 'type'].sort(),
    )
  })
})

describe('runBench record assembly', () => {
  it('produces a run header, one move row per item, and a summary', async () => {
    const fake = createFakeWorker()
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 3 },
      { createWorker: () => fake.worker },
    )
    const records = await promise

    expect(records[0].kind).toBe('run')
    expect(records[records.length - 1].kind).toBe('summary')
    expect(moveRows(records)).toHaveLength(3)

    const header = records[0] as BenchRunRecord
    expect(header.harness).toBe('device')
    expect(header.plan.positionCount).toBe(3)
    expect(header.depth.requested).toBe(3)

    const summary = records[records.length - 1] as BenchSummaryRecord
    expect(summary.runId).toBe(header.runId)
  })

  it('fills phases, cost totals, and the P===B flag from the transcript', async () => {
    const fake = createFakeWorker({ bestMove: 'd2d4' })
    const { promise } = runBench(singlePositionConfig, { createWorker: () => fake.worker })
    const [row] = moveRows(await promise)

    expect(row.phases.map((phase) => phase.name)).toEqual(['root', 'post-played', 'post-best'])
    expect(row.searchCount).toBe(3)
    expect(row.totalNodes).toBe(6000)
    expect(row.totalEngineMs).toBe(1200)
    expect(row.pEqualsB).toBe(false)
    expect(row.result?.bestMove).toBe('d2d4')
    expect(row.progressPings).toBe(1)
    expect(row.streamingPings).toBe(1)
    expect(row.error).toBeNull()
    expect(row.cohort).toBe('cold')
    expect(row.workerBootMs).not.toBeNull()
  })

  it('records two phases and P===B when the played move is the best move', async () => {
    const fake = createFakeWorker({ bestMove: 'e2e4' })
    const { promise } = runBench(singlePositionConfig, { createWorker: () => fake.worker })
    const [row] = moveRows(await promise)

    expect(row.pEqualsB).toBe(true)
    expect(row.searchCount).toBe(2)
  })
})

describe('runBench observer cost', () => {
  it('analyses the transcript after the response, not inside the latency it measures', async () => {
    // Every log event used to be tokenized, UCI-parsed, fed through snapshot
    // assembly and (for a short PV) a chess.js legality replay in the message
    // handler — and because the `analysis` response queues BEHIND those log
    // messages, all of that work sat inside the recorded end-to-end time. It also
    // scales with the number of info lines, so it would have charged whichever arm
    // searches more.
    let analysedBeforeResponse = -1
    const fake = createFakeWorker({
      onResponse: () => {
        analysedBeforeResponse = observer.feedLogCalls
      },
    })
    const { promise } = runBench(singlePositionConfig, { createWorker: () => fake.worker })
    const [row] = moveRows(await promise)

    expect(analysedBeforeResponse).toBe(0)
    // Deferred, not dropped: the same record comes out the other side.
    expect(observer.feedLogCalls).toBeGreaterThan(0)
    expect(row.phases.map((phase) => phase.name)).toEqual(['root', 'post-played', 'post-best'])
    expect(row.resetMs).not.toBeNull()
  })
})

describe('runBench failure handling', () => {
  it('moves the row after a worker rebuild into the cold cohort', async () => {
    // The schedule called this row warm, but its engine is a brand-new WASM
    // instance with an empty hash. Left in the warm cohort it would sit there as a
    // slow outlier and inflate the warm median — the number §11's gate reads.
    const counter = { n: 0 }
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 3, moveTimeoutMs: 20 },
      {
        createWorker: () =>
          createFakeWorker({ script: ['analysis', 'silent', 'analysis'], counter }).worker,
      },
    )
    const rows = moveRows(await promise)

    expect(rows.map((row) => row.cohort)).toEqual(['cold', 'warm', 'cold'])
    expect(rows.map((row) => row.workerRestarted)).toEqual([false, false, true])
    // The rebuild's boot cost belongs to that row, not to nobody.
    expect(rows[2].workerBootMs).not.toBeNull()
  })

  it('records no §4 rejection for a search that never answered bestmove', async () => {
    const { promise } = runBench(
      { ...singlePositionConfig, moveTimeoutMs: 20 },
      { createWorker: () => createFakeWorker({ script: ['open-search'] }).worker },
    )
    const [row] = moveRows(await promise)

    // The phase's cost is real and kept; its §4.2 acceptance is undefined, and
    // inventing a rejection here would feed §12 step 9 a counter the worker never
    // produced.
    expect(row.error).toMatch(/timed out/)
    expect(row.phases).toHaveLength(1)
    expect(row.phases[0].terminated).toBe(false)
    expect(row.phases[0].snapshot).toBeNull()
    expect(row.phases[0].nodes).toBe(1000)
    expect(Object.values(row.rejections).reduce((a, b) => a + b, 0)).toBe(0)
    expect(row.legacySelectorDivergence).toBe(0)
  })

  it('treats the worker rebuilding its own engine as a cold restart', async () => {
    // The worker's deadline-grace path destroys and recreates Stockfish but reports
    // a REQUEST-SCOPED error, indistinguishable from a bad FEN. Believing the
    // scoped error left the next row labelled `warm` with no boot cost while it ran
    // on a brand-new engine — the same warm-median inflation as a harness rebuild,
    // only invisible.
    const fake = createFakeWorker({ script: ['engine-rebuild', 'analysis'] })
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 2 },
      { createWorker: () => fake.worker },
    )
    const rows = moveRows(await promise)

    expect(rows[0].error).toMatch(/deadline grace/)
    expect(rows[0].engineRebuilt).toBe(true)
    expect(rows[1].cohort).toBe('cold')
    expect(rows[1].workerRestarted).toBe(true)
    expect(rows[1].workerBootMs).not.toBeNull()
    // The worker itself is fine, so it is NOT thrown away: one construction.
    expect(rows[1].engineRebuilt).toBe(false)
  })

  it('leaves engineRebuilt false when the worker only fails the request', async () => {
    const fake = createFakeWorker({ script: ['scoped-error', 'analysis'] })
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 2 },
      { createWorker: () => fake.worker },
    )
    const rows = moveRows(await promise)

    expect(rows[0].engineRebuilt).toBe(false)
    expect(rows[1].cohort).toBe('warm')
    expect(rows[1].workerRestarted).toBe(false)
  })

  it('records a scoped analysis error and keeps measuring', async () => {
    const fake = createFakeWorker({ script: ['scoped-error', 'analysis'] })
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 2 },
      { createWorker: () => fake.worker },
    )
    const rows = moveRows(await promise)

    expect(rows).toHaveLength(2)
    expect(rows[0].error).toBe('analysis failed')
    expect(rows[0].result).toBeNull()
    expect(rows[1].error).toBeNull()
  })

  it('bounds a silent worker with the move timeout and rebuilds it', async () => {
    // A timed-out move leaves engine state unknown, so the runner rebuilds the
    // worker; the fake factory hands out a fresh instance sharing one script.
    const counter = { n: 0 }
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 2, moveTimeoutMs: 20 },
      { createWorker: () => createFakeWorker({ script: ['silent', 'analysis'], counter }).worker },
    )
    const rows = moveRows(await promise)

    expect(rows[0].error).toMatch(/timed out/)
    expect(rows[1].error).toBeNull()
  })

  it('records every item as failed when the worker never becomes ready', async () => {
    const fake = createFakeWorker({ ready: false })
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 2, readyTimeoutMs: 10 },
      { createWorker: () => fake.worker },
    )
    const rows = moveRows(await promise)

    expect(rows).toHaveLength(2)
    expect(rows.every((row) => row.error?.startsWith('worker boot failed'))).toBe(true)
  })

  it('terminates the worker when the block ends', async () => {
    const fake = createFakeWorker()
    const { promise } = runBench(singlePositionConfig, { createWorker: () => fake.worker })
    await promise

    expect(fake.isTerminated()).toBe(true)
  })
})

describe('runBench stop', () => {
  it('stops between measurements and still summarizes what ran', async () => {
    const fake = createFakeWorker()
    let records: BenchRecord[] = []
    const handle = runBench(
      { ...singlePositionConfig, thermalPlies: 10 },
      {
        createWorker: () => fake.worker,
        onRecord: (record) => {
          records.push(record)
          if (moveRows(records).length === 2) handle.stop()
        },
      },
    )
    records = await handle.promise
    const summary = records[records.length - 1] as BenchSummaryRecord

    expect(moveRows(records).length).toBe(2)
    expect(summary.kind).toBe('summary')
    expect(summary.completion).toBe('stopped')
  })

  it('calls a run complete when stop lands on the final measurement', async () => {
    // `completion` is about whether the SCHEDULE was abandoned, not whether the
    // button was pressed. Stop during the last measurement still measures every
    // planned row, and marking that partial would discard good evidence.
    const fake = createFakeWorker()
    let records: BenchRecord[] = []
    const handle = runBench(
      { ...singlePositionConfig, thermalPlies: 2 },
      {
        createWorker: () => fake.worker,
        onRecord: (record) => {
          records.push(record)
          if (moveRows(records).length === 2) handle.stop()
        },
      },
    )
    records = await handle.promise
    const summary = records[records.length - 1] as BenchSummaryRecord

    expect(summary.completion).toBe('complete')
    expect(summary.measuredItems).toBe(summary.plannedItems)
    expect(summary.methodWarnings.join(' ')).not.toMatch(/stopped after/)
  })
})

describe('runBench progress', () => {
  it('reports monotonic progress and a terminal phase', async () => {
    const fake = createFakeWorker()
    const phases: string[] = []
    const dones: number[] = []
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 2 },
      {
        createWorker: () => fake.worker,
        onProgress: (progress) => {
          phases.push(progress.phase)
          dones.push(progress.done)
        },
      },
    )
    await promise

    expect(phases[0]).toBe('booting')
    expect(phases[phases.length - 1]).toBe('done')
    expect(dones[dones.length - 1]).toBe(2)
    expect([...dones].sort((a, b) => a - b)).toEqual(dones)
  })
})

describe('worker construction', () => {
  it('drives one position set item per measurement against the injected worker', async () => {
    const created: RunnerWorker[] = []
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 2, repeats: 2 },
      {
        createWorker: () => {
          const fake = createFakeWorker()
          created.push(fake.worker)
          return fake.worker
        },
      },
    )
    await promise

    // One fresh worker per (repeat, arm) block — that is what makes the cold
    // cohort meaningful.
    expect(created).toHaveLength(2)
    expect(moveRows(await promise).map((row) => row.cohort)).toEqual([
      'cold',
      'warm',
      'cold',
      'warm',
    ])
  })
})

describe('runBench method reporting', () => {
  it('records the plan, its provenance, and its method warnings in the header', async () => {
    const fake = createFakeWorker()
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 2, repeats: 1 },
      { createWorker: () => fake.worker },
    )
    const records = await promise
    const header = records[0] as BenchRunRecord
    const summary = records[records.length - 1] as BenchSummaryRecord

    expect(header.plan.plannedItems).toBe(2)
    expect(header.plan.armOrderBalanced).toBe(true)
    expect(header.plan.warmup).toBe(false)
    // Under vitest there is no build-time define, so the stamp is honestly empty
    // — and that itself is a warning, because an unidentified bundle is not
    // evidence.
    expect(header.source.gitRevision).toBeNull()
    expect(header.methodWarnings.join(' ')).toMatch(/repeats=1 is below/)
    expect(header.methodWarnings.join(' ')).toMatch(/no git revision/)
    expect(summary.completion).toBe('complete')
    expect(summary.methodWarnings).toEqual(expect.arrayContaining(header.methodWarnings))
  })

  it('warns that back-to-back repeats measure accumulated heat, on one arm too', async () => {
    // Three repeats of one arm are three blocks, of which only the first begins
    // cooled — and the by-move-index curve pools all three.
    const { promise } = runBench(
      { ...singlePositionConfig, repeats: 3 },
      { createWorker: () => createFakeWorker().worker },
    )
    const header = (await promise)[0] as BenchRunRecord

    expect(header.plan.blockCount).toBe(3)
    expect(header.methodWarnings.join(' ')).toMatch(/3 blocks run back-to-back with no cooldown/)
  })

  it('lets the driver stamp the worker bundle it measured', async () => {
    const fake = createFakeWorker()
    const { promise } = runBench(
      { ...singlePositionConfig, source: { gitRevision: 'abc123', gitDirty: false } },
      { createWorker: () => fake.worker },
    )
    const header = (await promise)[0] as BenchRunRecord

    expect(header.source.gitRevision).toBe('abc123')
    expect(header.methodWarnings.join(' ')).not.toMatch(/no git revision/)
  })
})

describe('runBench cooldown', () => {
  it('idles between blocks so a later repeat is not measured on the previous heat', async () => {
    const slept: number[] = []
    const { promise } = runBench(
      { ...singlePositionConfig, repeats: 3, blockCooldownMs: 60_000 },
      {
        createWorker: () => createFakeWorker().worker,
        sleep: async (ms) => {
          slept.push(ms)
        },
      },
    )
    await promise

    // Between blocks, not before the first: three blocks, two cooldowns.
    expect(slept).toEqual([60_000, 60_000])
  })
})

describe('runBench warm-up', () => {
  it('measures a priming row that no statistic counts', async () => {
    const fake = createFakeWorker()
    const { promise } = runBench(
      { ...singlePositionConfig, thermalPlies: 2, warmup: true },
      { createWorker: () => fake.worker },
    )
    const records = await promise
    const rows = moveRows(records)
    const summary = records[records.length - 1] as BenchSummaryRecord

    expect(rows.map((row) => [row.positionId, row.cohort, row.warmup])).toEqual([
      ['thermal:ply-001', 'cold', true],
      ['thermal:ply-001', 'warm', false],
      ['thermal:ply-002', 'warm', false],
    ])
    expect(summary.measuredItems).toBe(2)
    expect(summary.warmupItems).toBe(1)
    expect(summary.cells.some((cell) => cell.cohort === 'cold')).toBe(false)
  })
})

describe('runBench configuration guard', () => {
  it('refuses a cooldown it could not apply, instead of running without one', async () => {
    // `NaN > 0` is false everywhere, so this used to run every block back-to-back
    // AND report no cooldown warning, then serialize the plan field as `null`.
    let created = 0
    const { promise } = runBench(
      { ...singlePositionConfig, repeats: 3, blockCooldownMs: Number('nope') },
      {
        createWorker: () => {
          created += 1
          return createFakeWorker().worker
        },
      },
    )

    await expect(promise).rejects.toThrow(/invalid bench configuration.*blockCooldownMs/)
    // Refused before anything was measured, which is the point of doing it here.
    expect(created).toBe(0)
  })

  it('refuses a mode it would otherwise silently run as `sequence`', async () => {
    const { promise } = runBench(
      { ...singlePositionConfig, mode: 'seqence' as typeof singlePositionConfig.mode },
      { createWorker: () => createFakeWorker().worker },
    )

    await expect(promise).rejects.toThrow(/mode must be one of/)
  })
})

describe('start position sanity', () => {
  it('uses the checked-in FEN for the first thermal ply', async () => {
    const fake = createFakeWorker()
    const { promise } = runBench(singlePositionConfig, { createWorker: () => fake.worker })
    const [row] = moveRows(await promise)

    expect(row.fen).toBe(START_FEN)
    expect(row.playedMove).toBe('e2e4')
  })
})
