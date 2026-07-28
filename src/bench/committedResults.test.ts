import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { parseJsonl } from './benchRecord'
import type { BenchRunRecord, BenchSummaryRecord } from './benchRecord'
import { benchFileProblems } from './benchFile'
import {
  KILL_GATE_EVIDENCE,
  KILL_GATE_POSITION_SET_ID,
  killGateFile,
  killGateProblems,
  killGateVerdict,
} from './killGate'
import { buildPositionSet } from './device/positions'

/**
 * The committed benchmark files must be readable by the code that reads them,
 * and must still be the files the runs produced.
 *
 * This is not a formality: an all-cold run once emitted `observedMatchRate.m` as
 * NaN, which `JSON.stringify` wrote as `null` against a `number`-typed field — a
 * committed file that silently violated its own schema, and that nothing would
 * have caught until someone tried to quote it. `parseJsonl` validates every row
 * and refuses an unknown schema version, so running it over the checked-in files
 * is the cheapest possible guard against a baseline that cannot be read back.
 *
 * Per-row validity is not enough on its own: a file can lose move rows or gain an
 * edited summary and every remaining line still parses. `benchFileProblems`
 * rebuilds the summary from the rows and compares, which is what makes the
 * numbers in `docs/analysis/` accountable to the measurements beside them.
 */
const ANALYSIS_DIR = resolve(__dirname, '..', '..', 'docs', 'analysis')

const jsonlFiles = (): string[] => {
  try {
    return readdirSync(ANALYSIS_DIR)
      .filter((name) => name.endsWith('.jsonl'))
      .sort()
  } catch {
    return []
  }
}

describe('committed benchmark results', () => {
  const files = jsonlFiles()

  it('parses and validates every row of every file', () => {
    for (const file of files) {
      const records = parseJsonl(readFileSync(resolve(ANALYSIS_DIR, file), 'utf8'))
      expect(records.length, file).toBeGreaterThan(0)
    }
  })

  it('gives every file a run header and a summary that states its completion', () => {
    for (const file of files) {
      const records = parseJsonl(readFileSync(resolve(ANALYSIS_DIR, file), 'utf8'))
      const header = records.find((record) => record.kind === 'run') as BenchRunRecord | undefined
      const summary = records.find((record) => record.kind === 'summary') as
        | BenchSummaryRecord
        | undefined

      // A file with no summary is a crashed run, not a measurement.
      expect(header, file).toBeDefined()
      expect(summary, file).toBeDefined()
      expect(summary?.completion, file).toBe('complete')
      // §10.1: a dev-server run is a convenience check and must never be
      // committed as a baseline.
      expect(header?.build, file).toBe('bundled')
      expect(header?.source.gitRevision, file).not.toBeNull()
    }
  })

  it('holds together as a file: the summary is what its own move rows produce', () => {
    for (const file of files) {
      const records = parseJsonl(readFileSync(resolve(ANALYSIS_DIR, file), 'utf8'))
      expect(benchFileProblems(records), file).toEqual([])
    }
  })

  it('quotes a game-weighted median and p95, which §11 states its gate on', () => {
    for (const file of files) {
      const records = parseJsonl(readFileSync(resolve(ANALYSIS_DIR, file), 'utf8'))
      const summary = records.find((record) => record.kind === 'summary') as BenchSummaryRecord
      // An all-cold capture has no warm mixture and legitimately reports null;
      // what it must never do is omit the entry, leaving the gate unanswerable.
      expect(summary.gameWeighted.length, file).toBeGreaterThan(0)
      for (const entry of summary.gameWeighted) {
        expect(Object.keys(entry).sort(), file).toEqual([
          'arm',
          'm',
          'medianMs',
          'n',
          'p90Ms',
          'p95Ms',
          'worstMs',
        ])
      }
    }
  })

  /**
   * The kill-gate evidence (g-grade-kill-gate §5).
   *
   * Two pieces, because a directory scan must stay green BEFORE the capture
   * exists and must not silently pass afterwards:
   *
   * - DISCOVERED: any committed file whose header says `best-30`. Zero such files
   *   today, so there is nothing to check and this is green.
   * - REGISTERED: `KILL_GATE_EVIDENCE`, empty until the evidence commit, whose
   *   every entry must resolve to a discovered file. Filling it in the same
   *   commit as the JSONL is what turns this from vacuous into enforced, and
   *   deleting the file later fails the build instead of quietly reverting to
   *   zero discovered files.
   *
   * What it asserts is that the PRECONDITIONS hold and the verdict is
   * COMPUTABLE — deliberately not that it passes. A failing gate is a legitimate
   * verdict (§11's rejection clause requires the finding survive either way),
   * not a broken build.
   */
  describe('kill-gate evidence', () => {
    const gateFiles = files.filter((file) => {
      const records = parseJsonl(readFileSync(resolve(ANALYSIS_DIR, file), 'utf8'))
      const header = records.find((record) => record.kind === 'run') as BenchRunRecord | undefined
      return header?.plan.positionSetId === KILL_GATE_POSITION_SET_ID
    })
    const registered = Object.entries(KILL_GATE_EVIDENCE)

    it('registers every committed best-30 file, and only files that exist', () => {
      // Discovery keys on the SET ID, so the desktop control would be discovered
      // too if it were ever written here — it is a diagnostic, and this
      // directory holds evidence only.
      expect(gateFiles.sort(), 'unregistered best-30 files in docs/analysis/').toEqual(
        registered.map(([, filename]) => filename).sort(),
      )
    })

    it('holds every registered gate file to the §5 preconditions and computes its verdict', () => {
      const positionIds = buildPositionSet(KILL_GATE_POSITION_SET_ID).positions.map(
        (position) => position.positionId,
      )

      for (const [deviceLabel, filename] of registered) {
        const records = parseJsonl(readFileSync(resolve(ANALYSIS_DIR, filename), 'utf8'))
        const file = killGateFile(records)

        expect(killGateProblems(file, { deviceLabel, positionIds }), filename).toEqual([])
        // Computable, not passing: the verdict itself is recorded in the bead.
        expect(killGateVerdict(file), filename).not.toBeNull()
      }
    })
  })
})
