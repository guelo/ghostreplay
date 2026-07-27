/**
 * Whole-file checks for a benchmark JSONL (g-two-search-grade §10.1, §11).
 *
 * `validateBenchRecord` answers "is this row well-formed?", which is a per-line
 * property and cannot see that rows are MISSING or that the summary disagrees
 * with them. A file whose move rows were partly deleted, or whose summary was
 * edited, is a set of individually valid lines — and the numbers a reader quotes
 * come from the summary, so nothing about the shortfall is visible.
 *
 * The strongest available check is recomputation: the summary is a pure function
 * of the move rows plus three values the file itself carries, so it can simply be
 * rebuilt and compared. That catches an edited number, a dropped row, and a
 * summary that came from a different run, without enumerating the ways a file can
 * be wrong.
 *
 * Shared by both harnesses (§15.2 keeps it): the Node corpus harness writes the
 * same schema and must satisfy the same file-level invariants.
 */

import type {
  BenchMoveRecord,
  BenchRecord,
  BenchRunRecord,
  BenchSummaryRecord,
} from './benchRecord'
import { summarize } from './summarize'

/** Key-sorted JSON, so two structurally equal records compare as strings. */
const canonicalJson = (value: unknown): string => {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(',')}]`
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson((value as Record<string, unknown>)[key])}`)
    return `{${entries.join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

/**
 * Every way this file is internally inconsistent. Empty means it hangs together.
 *
 * Assumes each record already passed `validateBenchRecord` (i.e. came out of
 * `parseJsonl`).
 */
export const benchFileProblems = (records: readonly BenchRecord[]): string[] => {
  const problems: string[] = []

  if (records.length === 0) {
    return ['file is empty']
  }

  const runs = records.filter((record): record is BenchRunRecord => record.kind === 'run')
  const summaries = records.filter(
    (record): record is BenchSummaryRecord => record.kind === 'summary',
  )
  const moves = records.filter((record): record is BenchMoveRecord => record.kind === 'move')

  if (runs.length !== 1) {
    problems.push(`expected exactly 1 run header, found ${runs.length}`)
  } else if (records[0].kind !== 'run') {
    problems.push('the run header must be the first record')
  }
  if (summaries.length !== 1) {
    // No summary at all means the run crashed rather than finished — a real
    // state, but the file is then diagnostics and must not read as a measurement.
    problems.push(`expected exactly 1 summary, found ${summaries.length}`)
  } else if (records[records.length - 1].kind !== 'summary') {
    problems.push('the summary must be the last record')
  }
  if (runs.length !== 1 || summaries.length !== 1) {
    return problems
  }

  const header = runs[0]
  const summary = summaries[0]

  for (const record of records) {
    if (record.runId !== header.runId) {
      problems.push(`record with runId ${record.runId} does not belong to run ${header.runId}`)
      break
    }
  }

  // Contiguous from zero, in file order: `seq` is the runner's own counter, so a
  // gap is a row that was written and then removed.
  const gap = moves.findIndex((move, index) => move.seq !== index)
  if (gap !== -1) {
    problems.push(
      `move rows are not a contiguous sequence: row ${gap} has seq ${moves[gap].seq}`,
    )
  }

  for (const move of moves) {
    if (!header.plan.arms.includes(move.arm)) {
      problems.push(`move seq ${move.seq} has arm ${move.arm}, which the plan does not list`)
      break
    }
  }
  for (const move of moves) {
    if (move.blockIndex >= header.plan.blockCount) {
      problems.push(
        `move seq ${move.seq} is in block ${move.blockIndex}, past the plan's ${header.plan.blockCount}`,
      )
      break
    }
  }

  const accounted = summary.measuredItems + summary.warmupItems
  if (moves.length !== accounted) {
    problems.push(
      `summary accounts for ${accounted} rows (${summary.measuredItems} measured + ${summary.warmupItems} warm-up) but the file has ${moves.length}`,
    )
  }
  if (summary.plannedItems !== header.plan.plannedItems) {
    problems.push(
      `summary plans ${summary.plannedItems} measurements, the run header ${header.plan.plannedItems}`,
    )
  }
  // A run that finished its schedule measured all of it — every planned item
  // produces a row, errors included. Without this, deleting the LAST move row and
  // regenerating the summary leaves contiguous sequence numbers, matching counts,
  // a recomputable summary, and `complete` — the shortfall demoted to a method
  // warning on a file that still reads as quotable.
  if (summary.completion === 'complete' && summary.measuredItems !== summary.plannedItems) {
    problems.push(
      `summary says complete but measured ${summary.measuredItems} of ${summary.plannedItems} planned measurements`,
    )
  }

  // The summary is a pure function of the rows: rebuild it and compare. The
  // header's warnings are the plan half, which the summary repeats verbatim
  // before adding the outcome ones.
  const recomputed = summarize(header.runId, moves, {
    completion: summary.completion,
    plannedItems: summary.plannedItems,
    planWarnings: header.methodWarnings,
  })
  if (canonicalJson(recomputed) !== canonicalJson(summary)) {
    problems.push('the summary is not what these move rows produce')
  }

  return problems
}
