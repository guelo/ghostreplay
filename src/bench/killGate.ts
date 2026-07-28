/**
 * The mobile P===B kill gate (g-grade-kill-gate; g-two-search-grade §12 step 4).
 *
 * Variant A drops the post-best search on non-best moves but pays one extra root
 * ply on EVERY move, including the moves where the player already played the
 * engine's choice. On that cohort §3.4 gives it no saving at all — only the
 * extra ply — and the committed mobile baselines put `m` at 0.41. So if the
 * P===B cohort regresses badly on a phone, Variant A is dead regardless of how
 * good its correctness story is, and that is the cheapest possible kill of the
 * whole two-search idea.
 *
 * Two things live here, deliberately as pure functions over a whole FILE:
 *
 * - `killGateProblems` — the preconditions. Half of them are not row-level facts
 *   at all: the build and revision live in `run.build`/`run.source`, completion
 *   and method warnings in the summary, the plan in `run.plan`, and the device
 *   identity in `run.device`/`run.environment`. A `BenchMoveRecord[]`-only
 *   signature could check none of them, and would happily grade a dev-server run
 *   or a run that stopped half way.
 * - `killGateVerdict` — the arithmetic, and nothing else. A FAILING gate is a
 *   legitimate verdict, not a broken build (§11's rejection clause requires the
 *   finding survive either way), so the committed-results test asserts the
 *   verdict is COMPUTABLE, never that it passes.
 *
 * `expected` is passed IN rather than inferred, because every precondition below
 * is satisfiable by a desktop Chromium run: without a declared corpus and a
 * declared phone the gate would cheerfully accept the very control run §10.1
 * forbids as mobile evidence.
 */

import type {
  BenchArm,
  BenchLatencyStats,
  BenchMoveRecord,
  BenchRecord,
  BenchRunRecord,
  BenchSummaryRecord,
} from './benchRecord'
import { latencyStats, median, usableRows } from './summarize'

/** The position set that IS the gate corpus. Discovery keys on this. */
export const KILL_GATE_POSITION_SET_ID = 'best-30'

/** Both arms, in the order the run plan must name them. */
export const KILL_GATE_ARMS: readonly BenchArm[] = ['current', 'variantA']

/**
 * 4, not 3: `armOrderBalanced` needs a multiple of the arm count, and 3 repeats
 * over 2 arms hands one arm the opening slot twice.
 */
export const KILL_GATE_REPEATS = 4

/**
 * EXACTLY 60s, not "at least 30s".
 *
 * The looser form is satisfied by a run whose thermal method is simply a
 * different method: the cooldown is the control on cross-block heat, and a gate
 * comparing two arms under two cooling regimes measures the regime.
 */
export const KILL_GATE_COOLDOWN_MS = 60_000

/** The corpus was derived from depth-17 evidence, and the baselines are depth 17. */
export const KILL_GATE_DEPTH = 17

/** §12 step 4's threshold: at most a 10% median regression. */
export const KILL_GATE_MAX_REGRESSION = 0.1

/**
 * Committed gate evidence: declared device label → filename under
 * `docs/analysis/`.
 *
 * EMPTY until the evidence commit, and filling it in the same commit as the
 * JSONL is what turns the committed-results check from vacuous into enforced.
 * It also means deleting the file later fails the build rather than silently
 * reverting to "zero discovered files, nothing to check".
 *
 * The desktop control is NOT here and must not be written into
 * `docs/analysis/` at all — it is a diagnostic, and this directory holds
 * evidence only (see `scripts/bench/run-device-baseline.mjs`, which refuses a
 * `best-30` run without an explicit `--out`).
 */
export const KILL_GATE_EVIDENCE: Readonly<Record<string, string>> = {}

export type KillGateFile = {
  run: BenchRunRecord | null
  summary: BenchSummaryRecord | null
  moves: BenchMoveRecord[]
}

export type KillGateExpectation = {
  /**
   * The phone's exact `device.label`, DECLARED BEFORE THE CAPTURE.
   *
   * Matched as a string rather than sniffed from the user agent, which keeps the
   * check honest in both directions: a desktop file fails it, and so does a
   * phone file relabelled to look like the declared one only in prose.
   */
  deviceLabel: string
  /** The 30 `best30:` ids the run must have measured, once per repeat per arm. */
  positionIds: readonly string[]
}

/** Split a parsed JSONL file into the three record kinds the gate reads. */
export const killGateFile = (records: readonly BenchRecord[]): KillGateFile => ({
  run: (records.find((record) => record.kind === 'run') as BenchRunRecord) ?? null,
  summary:
    (records.find((record) => record.kind === 'summary') as BenchSummaryRecord) ?? null,
  moves: records.filter((record): record is BenchMoveRecord => record.kind === 'move'),
})

const same = (a: readonly string[], b: readonly string[]) =>
  a.length === b.length && a.every((value, index) => value === b[index])

/**
 * Every reason this file cannot decide the gate — all of them, not the first, so
 * a capture that has to be repeated is repeated once.
 *
 * A precondition failure makes the run VOID, not failed: it says nothing about
 * Variant A, and must never be recorded as a verdict.
 */
export const killGateProblems = (
  file: KillGateFile,
  expected: KillGateExpectation,
): string[] => {
  const problems: string[] = []
  const { run, summary, moves } = file

  if (!run) {
    problems.push('P0: no run header — a file with no header cannot name what it measured')
  }
  if (!summary) {
    // A file with no summary is a crashed run, not a measurement: its rows are
    // diagnostics, and their medians look entirely ordinary.
    problems.push('P0: no summary row — a crashed run is not a measurement')
  }
  if (!run || !summary) {
    return problems
  }

  // P1 — which orchestration bytes were measured.
  if (run.build !== 'bundled') {
    problems.push(`P1: build is ${JSON.stringify(run.build)}, not "bundled" (§10.1: a dev-server run is a convenience check)`)
  }
  if (run.source.gitRevision === null) {
    problems.push('P1: source.gitRevision is null — the file cannot name the bytes it measured')
  }
  if (run.source.gitDirty !== false) {
    problems.push(
      `P1: source.gitDirty is ${String(run.source.gitDirty)} — a dirty tree under-specifies the bundle, so the run is a working-copy measurement`,
    )
  }

  // P2 — did the run finish its plan, by its own method.
  if (summary.completion !== 'complete') {
    problems.push(`P2: summary.completion is ${JSON.stringify(summary.completion)} — only a complete run is quotable`)
  }
  if (summary.methodWarnings.length > 0) {
    problems.push(`P2: summary.methodWarnings is not empty: ${summary.methodWarnings.join('; ')}`)
  }
  if (summary.errors !== 0) {
    problems.push(`P2: summary.errors is ${summary.errors}, not 0`)
  }

  // P3 — the declared phone, and only it.
  if (run.harness !== 'device') {
    problems.push(`P3: harness is ${JSON.stringify(run.harness)}, not "device" (§10.1: Node timing is never mobile evidence)`)
  }
  if (run.device.label !== expected.deviceLabel) {
    problems.push(
      `P3: device.label is ${JSON.stringify(run.device.label)}, not the declared ${JSON.stringify(expected.deviceLabel)}`,
    )
  }
  for (const field of ['userAgent', 'platform', 'hardwareConcurrency'] as const) {
    if (run.environment[field] === null) {
      problems.push(`P3: environment.${field} is null — the declared device cannot be corroborated`)
    }
  }

  // P4 — the plan the gate is defined on.
  if (run.plan.positionSetId !== KILL_GATE_POSITION_SET_ID) {
    problems.push(
      `P4: plan.positionSetId is ${JSON.stringify(run.plan.positionSetId)}, not ${JSON.stringify(KILL_GATE_POSITION_SET_ID)}`,
    )
  }
  if (!same(run.plan.arms, KILL_GATE_ARMS)) {
    problems.push(`P4: plan.arms is [${run.plan.arms.join(', ')}], not [${KILL_GATE_ARMS.join(', ')}]`)
  }
  if (run.plan.repeats !== KILL_GATE_REPEATS) {
    problems.push(`P4: plan.repeats is ${run.plan.repeats}, not ${KILL_GATE_REPEATS}`)
  }
  if (!run.plan.armOrderBalanced) {
    problems.push('P4: plan.armOrderBalanced is false — one arm took the opening slot more often than the other')
  }
  if (!run.plan.warmup) {
    problems.push(
      'P4: plan.warmup is false — without it the block\'s cold row is always position 1, which every warm statistic then excludes, and the gate measures 29 positions',
    )
  }
  if (run.plan.blockCooldownMs !== KILL_GATE_COOLDOWN_MS) {
    problems.push(
      `P4: plan.blockCooldownMs is ${run.plan.blockCooldownMs}, not exactly ${KILL_GATE_COOLDOWN_MS} — the cooldown is the control on cross-block heat, and two arms under two cooling regimes measure the regime`,
    )
  }
  if (run.depth.requested !== run.depth.session) {
    problems.push(
      `P4: depth.requested (${run.depth.requested}) is not the device's session depth (${run.depth.session}) — a depth override is a different protocol`,
    )
  }
  if (run.depth.session !== KILL_GATE_DEPTH) {
    problems.push(
      `P4: depth.session is ${run.depth.session}, not ${KILL_GATE_DEPTH} — best-30's played moves ARE the engine's depth-${KILL_GATE_DEPTH} best moves, so at another depth the set is not the P===B cohort`,
    )
  }
  // The header's claim, checked against the rows rather than trusted. Nothing
  // else binds the two: `validateBenchRecord` checks each row in isolation,
  // `benchFileProblems` does not cross-reference depth, and `summarize` copies
  // no depth at all — so a run whose header says 17 while every row asked for 18
  // passes every other check in the codebase and is graded as if it were the
  // declared protocol. Every row, warm-ups included: a priming search at another
  // depth leaves a differently-warmed engine under the rows that follow it.
  const offDepth = moves.filter((row) => row.requestedDepth !== KILL_GATE_DEPTH)
  if (offDepth.length > 0) {
    // Aggregated: a depth override applies to the whole run, so one line per row
    // would bury every other problem under 240 copies of the same fact.
    const depths = [...new Set(offDepth.map((row) => row.requestedDepth))].sort((a, b) => a - b)
    problems.push(
      `P4: ${offDepth.length} row(s) asked for depth ${depths.join('/')}, not the declared ${KILL_GATE_DEPTH} (e.g. ${offDepth[0].arm} ${offDepth[0].positionId} repeat ${offDepth[0].repeat})`,
    )
  }

  const usable = usableRows(moves)

  // P5 — per arm, exactly one measurement of each position in each repeat.
  const expectedRepeats = Array.from({ length: KILL_GATE_REPEATS }, (_, index) => index)
  for (const arm of KILL_GATE_ARMS) {
    const armRows = usable.filter((row) => row.arm === arm && row.cohort === 'warm')
    // From 0, because `planBlocks` counts repeats from zero (`schedule.ts`): a
    // 1..4 gate would pass a synthetic fixture and reject the real capture.
    const repeats = [...new Set(armRows.map((row) => row.repeat))].sort((a, b) => a - b)
    if (!same(repeats.map(String), expectedRepeats.map(String))) {
      problems.push(
        `P5: ${arm} usable warm repeats are [${repeats.join(', ')}], not [${expectedRepeats.join(', ')}]`,
      )
      continue
    }
    for (const repeat of expectedRepeats) {
      const ids = armRows.filter((row) => row.repeat === repeat).map((row) => row.positionId)
      const counts = new Map<string, number>()
      for (const id of ids) counts.set(id, (counts.get(id) ?? 0) + 1)
      // Exact-once-per-repeat, NOT equal multisets across arms: both arms losing
      // position 7 and doubling position 8 matches, balances, and describes a
      // corpus that is not best-30.
      const missing = expected.positionIds.filter((id) => (counts.get(id) ?? 0) === 0)
      const repeated = expected.positionIds.filter((id) => (counts.get(id) ?? 0) > 1)
      const unexpected = [...counts.keys()].filter((id) => !expected.positionIds.includes(id))
      if (missing.length > 0 || repeated.length > 0 || unexpected.length > 0) {
        problems.push(
          `P5: ${arm} repeat ${repeat} does not measure each best-30 position exactly once` +
            (missing.length > 0 ? ` (missing ${missing.join(', ')})` : '') +
            (repeated.length > 0 ? ` (repeated ${repeated.join(', ')})` : '') +
            (unexpected.length > 0 ? ` (unexpected ${unexpected.join(', ')})` : ''),
        )
      }
    }
  }

  // P6 — best-30 IS the P===B cohort under the baseline protocol.
  const currentNotEqual = usable.filter((row) => row.arm === 'current' && row.pEqualsB !== true)
  if (currentNotEqual.length > 0) {
    problems.push(
      `P6: ${currentNotEqual.length} usable current row(s) have pEqualsB !== true (e.g. ${currentNotEqual[0].positionId}) — best-30 is not the P===B cohort in this run`,
    )
  }

  // P7 — Variant A's shape, on the transcript rather than on trust.
  //
  // The depths are ABSOLUTE — 18 and 17 — not `row.requestedDepth + 1` and
  // `row.requestedDepth`. Stated relatively they say only that Variant A did what
  // Variant A does at whatever depth it was handed, which a depth-18 run
  // satisfies with a 19/18 pair while the header still claims 17. What the gate
  // has to assert is the DECLARED protocol, so both ends are pinned to the same
  // constant P4 checks the header and the rows against.
  const rootDepth = KILL_GATE_DEPTH + 1
  for (const row of usable.filter((row) => row.arm === 'variantA')) {
    if (row.phases.length !== 2) {
      problems.push(`P7: variantA row ${row.positionId} (repeat ${row.repeat}) has ${row.phases.length} phases, not 2`)
      continue
    }
    const [root, played] = row.phases
    if (root.moves.length !== 0) {
      problems.push(`P7: variantA row ${row.positionId} phase 0 searched moves [${root.moves.join(' ')}], not the root`)
    }
    if (root.requestedDepth !== rootDepth) {
      problems.push(
        `P7: variantA row ${row.positionId} root depth is ${String(root.requestedDepth)}, not ${rootDepth} (N+1 at the declared N=${KILL_GATE_DEPTH})`,
      )
    }
    if (!same(played.moves, [row.playedMove])) {
      problems.push(
        `P7: variantA row ${row.positionId} phase 1 searched moves [${played.moves.join(' ')}], not [${row.playedMove}]`,
      )
    }
    if (played.requestedDepth !== KILL_GATE_DEPTH) {
      problems.push(
        `P7: variantA row ${row.positionId} post-played depth is ${String(played.requestedDepth)}, not the declared ${KILL_GATE_DEPTH}`,
      )
    }
  }

  // P8 — EVERY row, warm-ups included. `usableRows` drops them from the
  // statistics, which is right; a priming search that fired the cap or rebuilt
  // the engine still leaves a cold or freshly-rebuilt engine underneath the 30
  // measured rows that follow it in that block. Excluded from the numbers is not
  // the same as harmless to them.
  for (const row of moves) {
    const where = `${row.arm} ${row.positionId} (repeat ${row.repeat}${row.warmup ? ', warm-up' : ''})`
    if (!row.result) {
      // Positively required, not optional-chained. A row with `result: null` and
      // `error: null` — a measurement that neither answered nor reported why —
      // is dropped by `usableRows` (so P5 never counts it), is not an error (so
      // P2's `summary.errors` is still 0), and read through `?.` its absent
      // `capFired` was indistinguishable from an honest `false`. On a warm-up
      // that is a priming search which never completed, under 30 measured rows.
      problems.push(`P8: ${where} produced no result — the measurement did not complete`)
    } else if (row.result.capFired !== false) {
      problems.push(`P8: ${where} fired the analysis cap`)
    }
    if (row.workerRestarted) {
      problems.push(`P8: ${where} ran on a worker rebuilt after a failure`)
    }
    if (row.engineRebuilt) {
      problems.push(`P8: ${where} rebuilt its engine mid-measurement`)
    }
  }

  return problems
}

/** One arm's reported numbers. Never a filter — see `KillGateVerdict`. */
export type KillGateArmReport = {
  arm: BenchArm
  /** The gate cell: every usable warm row of the FIXED corpus. */
  warm: BenchLatencyStats
  /** Reported for context; the gate is never read off these. */
  warmPEqualsB: BenchLatencyStats | null
  warmPDiffers: BenchLatencyStats | null
  /** Median engine time and nodes per phase index (0 = root, 1 = post-played). */
  phases: Array<{ index: number; medianEngineMs: number | null; medianNodes: number | null }>
}

export type KillGateVerdict = {
  /** `median(variantA) / median(current) - 1`, over the fixed corpus. */
  regression: number
  pass: boolean
  medians: { current: number; variantA: number }
  /**
   * Variant A rows whose depth-18 root named a DIFFERENT best move than the
   * corpus's played move.
   *
   * Those rows stay IN the comparison: §3.4 gives Variant A the same cost on
   * both splits (`R(N+1) + S(N)`), so excluding them would drop real
   * measurements for a reason that does not affect cost. This count is a FINDING
   * for g-grade-variant-b, never a filter.
   */
  disagreements: number
  reported: KillGateArmReport[]
}

const medianOrNull = (values: readonly number[]): number | null =>
  values.length > 0 ? median(values) : null

const armReport = (rows: readonly BenchMoveRecord[], arm: BenchArm): KillGateArmReport => {
  const warm = rows.filter((row) => row.arm === arm && row.cohort === 'warm')
  const equal = warm.filter((row) => row.pEqualsB === true)
  const differs = warm.filter((row) => row.pEqualsB === false)
  const phaseCount = Math.max(0, ...warm.map((row) => row.phases.length))

  return {
    arm,
    warm: latencyStats(warm),
    warmPEqualsB: equal.length > 0 ? latencyStats(equal) : null,
    warmPDiffers: differs.length > 0 ? latencyStats(differs) : null,
    phases: Array.from({ length: phaseCount }, (_, index) => ({
      index,
      medianEngineMs: medianOrNull(
        warm
          .map((row) => row.phases[index]?.timeMs)
          .filter((value): value is number => typeof value === 'number'),
      ),
      medianNodes: medianOrNull(
        warm
          .map((row) => row.phases[index]?.nodes)
          .filter((value): value is number => typeof value === 'number'),
      ),
    })),
  }
}

/**
 * The gate arithmetic, read off the FIXED corpus rather than off each arm's own
 * `p-equals-b` cell.
 *
 * `pEqualsB` is computed from EACH ARM'S OWN returned best move
 * (`runner.ts`) and `summarize` splits each arm independently
 * (`summarize.ts`) — so on the rows where Variant A's depth-18 root renames `B`,
 * its row leaves the p-equals-b cell while `current`'s stays, and the two arms'
 * p-equals-b medians would describe different position sets. Comparing them is
 * the one way this gate can quietly answer the wrong question.
 *
 * Null when either arm has no usable warm rows: a gate with nothing to divide is
 * unanswerable, not passing.
 */
export const killGateVerdict = (file: KillGateFile): KillGateVerdict | null => {
  const usable = usableRows(file.moves)
  const reported = KILL_GATE_ARMS.map((arm) => armReport(usable, arm))
  const current = reported.find((entry) => entry.arm === 'current')
  const variantA = reported.find((entry) => entry.arm === 'variantA')

  if (!current || !variantA || current.warm.n === 0 || variantA.warm.n === 0) {
    return null
  }

  const regression = variantA.warm.medianMs / current.warm.medianMs - 1

  return {
    regression,
    // Stated as a multiplication rather than as `regression <= MAX`. The two
    // agree everywhere except the last ulp, and only this form makes an
    // EXACTLY 10% regression pass, which is what "at most 10%" says: in
    // doubles, 1100 / 1000 - 1 is 0.10000000000000009, so the division form
    // would reject the parent epic on floating-point noise.
    pass: variantA.warm.medianMs <= current.warm.medianMs * (1 + KILL_GATE_MAX_REGRESSION),
    medians: { current: current.warm.medianMs, variantA: variantA.warm.medianMs },
    disagreements: usable.filter((row) => row.arm === 'variantA' && row.pEqualsB === false).length,
    reported,
  }
}
