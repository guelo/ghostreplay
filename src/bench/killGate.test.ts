import { describe, expect, it } from 'vitest'
import type {
  BenchArm,
  BenchMoveRecord,
  BenchPhaseRecord,
  BenchRecord,
  BenchRunRecord,
} from './benchRecord'
import { BENCH_SCHEMA_VERSION, zeroedDivergences, zeroedRejections } from './benchRecord'
import { summarize } from './summarize'
import { buildPositionSet } from './device/positions'
import { counterbalancedArms } from './device/schedule'
import {
  KILL_GATE_ARMS,
  KILL_GATE_COOLDOWN_MS,
  KILL_GATE_DEPTH,
  KILL_GATE_REPEATS,
  killGateFile,
  killGateProblems,
  killGateVerdict,
} from './killGate'

/**
 * A synthetic gate file, and one deviation at a time.
 *
 * Each precondition has to fail INDEPENDENTLY — a gate that only rejects a file
 * failing several at once would wave through the realistic failures, which are
 * one thing being subtly wrong. Four of them get their own test because the
 * obvious looser form of the check is satisfied by data that would move the
 * median: a desktop-labelled file (P3), a 30s cooldown (P4), a repeat that loses
 * one position and doubles another so the multiset still matches (P5), and a
 * warm-up row that fired the cap (P8).
 */

const POSITIONS = buildPositionSet('best-30').positions
const POSITION_IDS = POSITIONS.map((position) => position.positionId)
const DEVICE_LABEL = 'iPhone XR, iOS 17.7, Safari'
const EXPECTED = { deviceLabel: DEVICE_LABEL, positionIds: POSITION_IDS }

/** Flat latencies per arm, so each arm's median IS this number. */
const LATENCY: Record<string, number> = { current: 1000, variantA: 1050 }

const phase = (
  index: number,
  moves: string[],
  requestedDepth: number,
): BenchPhaseRecord => ({
  index,
  name: index === 0 ? 'root' : 'post-played',
  moves,
  requestedDepth,
  bestmove: 'e2e4',
  nodes: 1000,
  timeMs: 500,
  nps: 2000,
  hashfull: 100,
  reachedDepth: requestedDepth,
  seldepth: requestedDepth + 4,
  wallMs: 510,
  infoLines: 10,
  admittedLines: 10,
  terminated: true,
  snapshot: { accepted: true, depth: requestedDepth },
  legacyDivergence: null,
  stopObserved: false,
})

type RowOptions = {
  runId: string
  seq: number
  blockIndex: number
  repeat: number
  arm: BenchArm
  orderIndex: number
  positionIndex: number
  warmup: boolean
  cohort: 'cold' | 'warm'
}

const moveRow = (options: RowOptions): BenchMoveRecord => {
  const position = POSITIONS[options.positionIndex]
  const depth = KILL_GATE_DEPTH
  const phases =
    options.arm === 'variantA'
      ? [phase(0, [], depth + 1), phase(1, [position.playedMove], depth)]
      : [phase(0, [], depth), phase(1, [position.playedMove], depth)]

  return {
    kind: 'move',
    schemaVersion: BENCH_SCHEMA_VERSION,
    runId: options.runId,
    seq: options.seq,
    blockIndex: options.blockIndex,
    repeat: options.repeat,
    arm: options.arm,
    orderIndex: options.orderIndex,
    positionId: position.positionId,
    fen: position.fen,
    playedMove: position.playedMove,
    playerColor: position.playerColor,
    thermalIndex: null,
    cohort: options.cohort,
    warmup: options.warmup,
    workerRestarted: false,
    engineRebuilt: false,
    requestedDepth: depth,
    e2eMs: LATENCY[options.arm],
    runElapsedMs: options.seq * 1000,
    workerBootMs: options.seq === 0 ? 1200 : null,
    resetMs: 5,
    phases,
    searchCount: phases.length,
    totalNodes: 2000,
    totalEngineMs: 1000,
    result: {
      bestMove: position.playedMove,
      bestLine: [position.playedMove],
      bestEval: 20,
      playedEval: 20,
      bestEvalMate: null,
      playedEvalMate: null,
      delta: 0,
      classification: 'best',
      canonical: true,
      capFired: false,
      stopReason: 'bestmove',
      reachedDepth: depth,
    },
    pEqualsB: true,
    rejections: zeroedRejections(),
    legacySelectorDivergence: 0,
    divergenceByReason: zeroedDivergences(),
    progressPings: 4,
    streamingPings: 2,
    error: null,
  }
}

const runHeader = (runId: string): BenchRunRecord => ({
  kind: 'run',
  schemaVersion: BENCH_SCHEMA_VERSION,
  runId,
  harness: 'device',
  build: 'bundled',
  startedAtIso: '2026-07-28T09:00:00.000Z',
  engine: {
    engineVersion: '18-lite',
    engineBuild: 'single',
    evalFileId: 'nn-x',
    threads: 1,
    hashMb: 128,
  },
  depth: { baseline: 17, maxDevice: 17, session: KILL_GATE_DEPTH, requested: KILL_GATE_DEPTH },
  environment: {
    userAgent: 'Mozilla/5.0 (iPhone)',
    uaData: null,
    hardwareConcurrency: 6,
    deviceMemory: null,
    platform: 'iPhone',
    screen: '828x1792',
    devicePixelRatio: 2,
    timeZone: 'UTC',
  },
  source: {
    gitRevision: 'abc123',
    gitDirty: false,
    workerBundleFile: null,
    workerBundleSha256: null,
  },
  device: { label: DEVICE_LABEL, notes: 'cooled, plugged in' },
  plan: {
    mode: 'sequence',
    arms: [...KILL_GATE_ARMS],
    repeats: KILL_GATE_REPEATS,
    positionSetId: 'best-30',
    positionCount: POSITIONS.length,
    blockCount: KILL_GATE_REPEATS * KILL_GATE_ARMS.length,
    plannedItems: KILL_GATE_REPEATS * KILL_GATE_ARMS.length * POSITIONS.length,
    warmup: true,
    armOrderBalanced: true,
    blockCooldownMs: KILL_GATE_COOLDOWN_MS,
  },
  methodWarnings: [],
  countersAre: 'observer-recomputed',
})

/** The real schedule shape: 8 blocks of (1 priming + 30 measured). */
const buildRows = (runId: string): BenchMoveRecord[] => {
  const rows: BenchMoveRecord[] = []
  let seq = 0
  let blockIndex = 0
  for (let repeat = 0; repeat < KILL_GATE_REPEATS; repeat += 1) {
    counterbalancedArms([...KILL_GATE_ARMS], repeat).forEach((arm, orderIndex) => {
      rows.push(
        moveRow({ runId, seq: seq++, blockIndex, repeat, arm, orderIndex, positionIndex: 0, warmup: true, cohort: 'cold' }),
      )
      POSITIONS.forEach((_, positionIndex) => {
        rows.push(
          moveRow({ runId, seq: seq++, blockIndex, repeat, arm, orderIndex, positionIndex, warmup: false, cohort: 'warm' }),
        )
      })
      blockIndex += 1
    })
  }
  return rows
}

const buildFile = (
  mutate: (parts: { run: BenchRunRecord; rows: BenchMoveRecord[] }) => void = () => {},
): BenchRecord[] => {
  const runId = 'gate-run'
  const parts = { run: runHeader(runId), rows: buildRows(runId) }
  mutate(parts)
  const summary = summarize(runId, parts.rows, {
    completion: 'complete',
    plannedItems: parts.run.plan.plannedItems,
    planWarnings: parts.run.methodWarnings,
  })
  return [parts.run, ...parts.rows, summary]
}

const problemsFor = (
  mutate?: (parts: { run: BenchRunRecord; rows: BenchMoveRecord[] }) => void,
) => killGateProblems(killGateFile(buildFile(mutate)), EXPECTED)

describe('kill-gate preconditions', () => {
  it('accepts a clean capture and counts what it measured', () => {
    const records = buildFile()

    expect(problemsFor()).toEqual([])
    // 8 blocks x (1 priming + 30 measured) = 248 rows.
    expect(records.filter((record) => record.kind === 'move')).toHaveLength(248)
  })

  it('P0: refuses a file with no summary row', () => {
    const records = buildFile().filter((record) => record.kind !== 'summary')

    expect(killGateProblems(killGateFile(records), EXPECTED).join(' ')).toMatch(
      /P0: no summary row/,
    )
  })

  it('P0: refuses a file with no run header', () => {
    const records = buildFile().filter((record) => record.kind !== 'run')

    expect(killGateProblems(killGateFile(records), EXPECTED).join(' ')).toMatch(
      /P0: no run header/,
    )
  })

  it('P1: refuses a dev build, a missing revision, or a dirty tree', () => {
    expect(problemsFor(({ run }) => { run.build = 'dev' }).join(' ')).toMatch(/P1: build/)
    expect(problemsFor(({ run }) => { run.source.gitRevision = null }).join(' ')).toMatch(/P1: source.gitRevision/)
    expect(problemsFor(({ run }) => { run.source.gitDirty = true }).join(' ')).toMatch(/P1: source.gitDirty/)
  })

  it('P2: refuses a stopped run, a method warning, or any error', () => {
    const stopped = buildFile()
    const summary = stopped.find((record) => record.kind === 'summary')!
    ;(summary as { completion: string }).completion = 'stopped'
    expect(killGateProblems(killGateFile(stopped), EXPECTED).join(' ')).toMatch(/P2: summary.completion/)

    expect(problemsFor(({ run }) => { run.methodWarnings = ['too few repeats'] }).join(' ')).toMatch(
      /P2: summary.methodWarnings/,
    )
    expect(
      problemsFor(({ rows }) => {
        rows[10].error = 'analyze-move timed out'
      }).join(' '),
    ).toMatch(/P2: summary.errors/)
  })

  it('P3: refuses a desktop-labelled file even though every other check passes', () => {
    // The whole reason the phone is DECLARED before the capture: every other
    // precondition here is satisfiable by a desktop Chromium run, so without a
    // declared identity the gate would accept the very control run §10.1 forbids
    // as mobile evidence.
    const problems = problemsFor(({ run }) => {
      run.device.label = 'MacBook Pro M1, macOS 15, Chromium'
    })

    expect(problems).toHaveLength(1)
    expect(problems[0]).toMatch(/P3: device.label/)
  })

  it('P3: refuses a Node-harness file and an unidentified environment', () => {
    expect(problemsFor(({ run }) => { run.harness = 'node' }).join(' ')).toMatch(/P3: harness/)
    expect(problemsFor(({ run }) => { run.environment.userAgent = null }).join(' ')).toMatch(
      /P3: environment.userAgent/,
    )
  })

  it('P4: refuses a 30s cooldown, which is a different thermal method', () => {
    // `>= 30000` would accept this. The cooldown is the control on cross-block
    // heat, and a gate comparing two arms under two cooling regimes measures the
    // regime.
    const problems = problemsFor(({ run }) => {
      run.plan.blockCooldownMs = 30_000
    })

    expect(problems).toHaveLength(1)
    expect(problems[0]).toMatch(/P4: plan.blockCooldownMs is 30000/)
  })

  it('P4: refuses a wrong set, wrong arms, wrong repeats, no warm-up, or an unbalanced order', () => {
    expect(problemsFor(({ run }) => { run.plan.positionSetId = 'thermal-40' }).join(' ')).toMatch(/P4: plan.positionSetId/)
    expect(problemsFor(({ run }) => { run.plan.arms = ['current'] }).join(' ')).toMatch(/P4: plan.arms/)
    expect(problemsFor(({ run }) => { run.plan.repeats = 3 }).join(' ')).toMatch(/P4: plan.repeats is 3/)
    expect(problemsFor(({ run }) => { run.plan.warmup = false }).join(' ')).toMatch(/P4: plan.warmup/)
    expect(problemsFor(({ run }) => { run.plan.armOrderBalanced = false }).join(' ')).toMatch(/P4: plan.armOrderBalanced/)
  })

  it('P4: refuses a depth override and a non-17 session depth', () => {
    expect(problemsFor(({ run }) => { run.depth.requested = 21 }).join(' ')).toMatch(/P4: depth.requested/)
    expect(
      problemsFor(({ run }) => {
        run.depth.session = 18
        run.depth.requested = 18
      }).join(' '),
    ).toMatch(/P4: depth.session is 18/)
  })

  it('P4/P7: refuses rows whose own depth contradicts the header', () => {
    // A run that actually measured depth 18 under a header still claiming 17.
    // Stated RELATIVELY, P7's "root is N+1, post-played is N" is satisfied by the
    // 19/18 pair such a run produces — and nothing else in the codebase binds a
    // row's depth to the header it was written under: `validateBenchRecord`
    // checks rows in isolation, `benchFileProblems` does not cross-reference
    // depth, and `summarize` copies no depth at all.
    const problems = problemsFor(({ rows }) => {
      for (const row of rows) {
        row.requestedDepth = KILL_GATE_DEPTH + 1
        row.phases[0].requestedDepth =
          row.arm === 'variantA' ? KILL_GATE_DEPTH + 2 : KILL_GATE_DEPTH + 1
        row.phases[1].requestedDepth = KILL_GATE_DEPTH + 1
      }
    })
    const joined = problems.join(' ')

    // One aggregated line for the run-wide fact, not 248 copies of it.
    expect(joined).toMatch(/P4: 248 row\(s\) asked for depth 18, not the declared 17/)
    expect(problems.filter((problem) => problem.startsWith('P4:'))).toHaveLength(1)
    // And P7 now reads the absolute depths, so the self-consistent 19/18 pair is
    // refused rather than accepted as "Variant A's shape".
    expect(joined).toMatch(/P7: .* root depth is 19, not 18 \(N\+1 at the declared N=17\)/)
    expect(joined).toMatch(/P7: .* post-played depth is 18, not the declared 17/)
  })

  it('P5: refuses a repeat that loses one position and doubles another', () => {
    // Equal multisets are satisfied by BOTH arms losing position 7 and
    // double-counting position 8 — matched, balanced, and describing a corpus
    // that is not best-30. Exact-once-per-repeat is the property the design
    // actually claims.
    const problems = problemsFor(({ rows }) => {
      for (const arm of KILL_GATE_ARMS) {
        const victim = rows.find(
          (row) => row.arm === arm && row.repeat === 0 && !row.warmup && row.positionId === POSITION_IDS[7],
        )!
        victim.positionId = POSITION_IDS[8]
      }
    })

    expect(problems).toHaveLength(2)
    for (const problem of problems) {
      expect(problem).toMatch(/P5: .* repeat 0 does not measure each best-30 position exactly once/)
      expect(problem).toContain(`missing ${POSITION_IDS[7]}`)
      expect(problem).toContain(`repeated ${POSITION_IDS[8]}`)
    }
  })

  it('P5: counts repeats from 0, as planBlocks does', () => {
    // A 1..4 gate passes a synthetic fixture numbered from 1 and rejects the
    // real capture, which is the wrong way round for a check that only ever runs
    // against a real capture.
    const problems = problemsFor(({ rows }) => {
      for (const row of rows) row.repeat += 1
    })

    expect(problems.join(' ')).toMatch(/P5: current usable warm repeats are \[1, 2, 3, 4\]/)
  })

  it('P6: refuses a corpus that is not the P===B cohort under the current protocol', () => {
    const problems = problemsFor(({ rows }) => {
      const row = rows.find((entry) => entry.arm === 'current' && !entry.warmup)!
      row.pEqualsB = false
    })

    expect(problems.join(' ')).toMatch(/P6: 1 usable current row/)
  })

  it('P7: refuses a variantA row whose shape is not root N+1 then played N', () => {
    expect(
      problemsFor(({ rows }) => {
        const row = rows.find((entry) => entry.arm === 'variantA' && !entry.warmup)!
        row.phases[0].requestedDepth = KILL_GATE_DEPTH
      }).join(' '),
    ).toMatch(/P7: .* root depth is 17, not 18/)

    expect(
      problemsFor(({ rows }) => {
        const row = rows.find((entry) => entry.arm === 'variantA' && !entry.warmup)!
        row.phases.push(phase(2, ['e2e4'], KILL_GATE_DEPTH))
      }).join(' '),
    ).toMatch(/P7: .* has 3 phases, not 2/)

    expect(
      problemsFor(({ rows }) => {
        const row = rows.find((entry) => entry.arm === 'variantA' && !entry.warmup)!
        row.phases[1].moves = []
      }).join(' '),
    ).toMatch(/P7: .* phase 1 searched moves \[\]/)
  })

  it('P8: refuses a WARM-UP row that fired the cap', () => {
    // `usableRows` drops warm-ups from the statistics, which is right — but a
    // priming search that fired the cap leaves a cold engine underneath the 30
    // measured rows behind it in that block. Excluded from the numbers is not
    // the same as harmless to them.
    const problems = problemsFor(({ rows }) => {
      const warmup = rows.find((row) => row.warmup)!
      warmup.result!.capFired = true
    })

    expect(problems).toHaveLength(1)
    expect(problems[0]).toMatch(/P8: .*warm-up.* fired the analysis cap/)
  })

  it('P8: refuses a row that produced no result at all', () => {
    // The gap an optional chain left open: `result: null` with `error: null` is
    // dropped by `usableRows` (so P5 never counts it), is not an error (so P2's
    // `summary.errors` stays 0), and read through `?.capFired` its absent cap
    // flag was indistinguishable from an honest `false`. On a warm-up that is a
    // priming search which never completed, under 30 measured rows.
    const problems = problemsFor(({ rows }) => {
      rows.find((row) => row.warmup)!.result = null
    })

    expect(problems).toHaveLength(1)
    expect(problems[0]).toMatch(/P8: .*warm-up.* produced no result/)
  })

  it('P8: refuses a rebuilt engine or a restarted worker on any row', () => {
    expect(problemsFor(({ rows }) => { rows[40].engineRebuilt = true }).join(' ')).toMatch(
      /P8: .* rebuilt its engine mid-measurement/,
    )
    expect(problemsFor(({ rows }) => { rows[40].workerRestarted = true }).join(' ')).toMatch(
      /P8: .* ran on a worker rebuilt after a failure/,
    )
  })
})

describe('kill-gate verdict', () => {
  const verdictFor = (variantAMs: number) => {
    LATENCY.variantA = variantAMs
    try {
      return killGateVerdict(killGateFile(buildFile()))
    } finally {
      LATENCY.variantA = 1050
    }
  }

  it('reads the regression off the fixed corpus, per arm', () => {
    const verdict = verdictFor(1050)!

    expect(verdict.medians).toEqual({ current: 1000, variantA: 1050 })
    expect(verdict.regression).toBeCloseTo(0.05, 12)
    expect(verdict.pass).toBe(true)
    // 4 repeats x 30 positions, warm-ups excluded.
    expect(verdict.reported.find((entry) => entry.arm === 'variantA')!.warm.n).toBe(120)
  })

  it('passes at exactly 10% and fails just past it', () => {
    // In doubles 1100 / 1000 - 1 is 0.10000000000000009, so a naive
    // `regression <= 0.10` would reject the parent epic on floating-point noise.
    expect(verdictFor(1100)!.pass).toBe(true)
    expect(verdictFor(1101)!.pass).toBe(false)
  })

  it('reports Variant A’s depth-18 disagreements without filtering on them', () => {
    LATENCY.variantA = 1050
    const records = buildFile(({ rows }) => {
      for (const row of rows.filter((entry) => entry.arm === 'variantA' && !entry.warmup).slice(0, 7)) {
        row.pEqualsB = false
        // A disagreeing row is SLOWER, and it stays in the median: §3.4 gives
        // Variant A the same cost on both splits, so excluding it would drop a
        // real measurement for a reason that does not affect cost.
        row.e2eMs = 5000
      }
    })
    const verdict = killGateVerdict(killGateFile(records))!

    expect(verdict.disagreements).toBe(7)
    expect(verdict.reported.find((entry) => entry.arm === 'variantA')!.warm.n).toBe(120)
    expect(verdict.reported.find((entry) => entry.arm === 'variantA')!.warmPDiffers?.n).toBe(7)
    expect(verdict.medians.variantA).toBe(1050)
  })

  it('reports per-phase engine time and nodes for both searches', () => {
    const verdict = verdictFor(1050)!
    const variantA = verdict.reported.find((entry) => entry.arm === 'variantA')!

    expect(variantA.phases.map((entry) => entry.index)).toEqual([0, 1])
    expect(variantA.phases[0].medianEngineMs).toBe(500)
    expect(variantA.phases[1].medianNodes).toBe(1000)
  })

  it('is unanswerable rather than passing when an arm has no warm rows', () => {
    const records = buildFile(({ rows }) => {
      for (const row of rows.filter((entry) => entry.arm === 'variantA')) {
        row.error = 'analyze-move timed out'
        row.result = null
      }
    })

    expect(killGateVerdict(killGateFile(records))).toBeNull()
  })
})
