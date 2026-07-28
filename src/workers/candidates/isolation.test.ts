import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { CANDIDATE_PROTOCOLS, availableCandidateArms } from './index'
import { isCandidateArm } from './benchMessages'

/**
 * §15.1's structure, checked structurally.
 *
 * C3, C4 and C8 are claims about what candidate code CANNOT do, and the cheapest
 * honest proof of "cannot" is that the capability is absent from the source
 * rather than merely unused by today's arm. A behavioural test would pass just
 * as well against an arm that posts a response on a branch nobody exercised.
 *
 * This is also what keeps §15.2's removal cheap: everything the rejection
 * verdict deletes is in this one directory.
 */
const HERE = __dirname

const productionSources = (): string[] =>
  readdirSync(HERE, { withFileTypes: true })
    .filter((entry) => entry.isFile() && /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name))
    .map((entry) => entry.name)
    .sort()

/**
 * Source with comments removed.
 *
 * The checks below are about what candidate code CAN DO, and these modules
 * document what they deliberately cannot — `contract.ts` names
 * `ctx.postMessage` and `activeSearch` precisely to say an arm never sees them.
 * Grepping the raw text would make writing that down a test failure, which is
 * the wrong incentive entirely.
 */
const stripComments = (source: string) =>
  source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')

const read = (file: string) => stripComments(readFileSync(resolve(HERE, file), 'utf8'))

describe('candidate isolation (§15.1)', () => {
  it('has every candidate module under this one directory (C2)', () => {
    // The §15.2 delete list, as it actually exists on disk.
    expect(productionSources()).toEqual([
      'benchMessages.ts',
      'contract.ts',
      'index.ts',
      'variantA.ts',
    ])
  })

  it('never posts a response (C3)', () => {
    for (const file of productionSources()) {
      // Candidates return the shared tail's inputs and stop. Naming
      // `postMessage` at all would mean a second way for an arm to reach the
      // host, which is the thing C3 rules out.
      expect(read(file), file).not.toMatch(/postMessage/)
    }
  })

  it('owns no lifecycle state (C4)', () => {
    const forbidden = [
      'activeSearch',
      'heartbeat',
      'resetAckQueue',
      'canceledAnalyses',
      'engineReady',
      'AnalysisBudget',
      'deadlineAt',
    ]
    for (const file of productionSources()) {
      for (const token of forbidden) {
        expect(read(file), `${file} names ${token}`).not.toContain(token)
      }
    }
  })

  it('constructs no provenance or producer token (C8)', () => {
    for (const file of productionSources()) {
      expect(read(file), file).not.toMatch(/buildBrowserProvenance|browser-analyzer-v|browser-restricted-multipv/)
    }
  })

  it('cannot make itself evidence-eligible (C8)', () => {
    // The arm never states eligibility at all: the shared join hardcodes `false`
    // for every candidate, on every path. An arm that could set the field would
    // be one refactor away from setting it true.
    for (const file of productionSources()) {
      expect(read(file), file).not.toContain('evidenceEligible')
    }
    expect(readFileSync(resolve(HERE, '..', 'analysisWorker.ts'), 'utf8')).toContain(
      'candidate === null && !outcome.capFired && canonical',
    )
  })

  it('cannot express an arm on the production analyze-move (C7)', () => {
    // The selector lives on the bench-only message type. Widening
    // `AnalyzeMoveMessage` with it — even optionally — would put it on the type
    // every in-game caller builds, and C7's first key would stop being a key.
    const production = stripComments(
      readFileSync(resolve(HERE, '..', 'analysisMessages.ts'), 'utf8'),
    )
    expect(production).not.toMatch(/^\s*arm\??\s*:/m)
    expect(stripComments(readFileSync(resolve(HERE, 'benchMessages.ts'), 'utf8'))).toMatch(
      /^\s*arm\?\s*:/m,
    )
  })

  it('advertises exactly the arms it can dispatch', () => {
    expect(availableCandidateArms()).toEqual(['variantA'])
    expect(Object.keys(CANDIDATE_PROTOCOLS)).toEqual(['variantA'])
    // variantB is a KNOWN arm the registry does not hold — a distinct state from
    // an unknown string, and the reason `bench-ready` advertises a list rather
    // than a boolean.
    expect(isCandidateArm('variantB')).toBe(true)
    expect(CANDIDATE_PROTOCOLS.variantB).toBeUndefined()
    expect(isCandidateArm('variantC')).toBe(false)
    expect(isCandidateArm('current')).toBe(false)
  })

  it('gives every registered arm the protocol name it is registered under', () => {
    for (const [arm, protocol] of Object.entries(CANDIDATE_PROTOCOLS)) {
      // The response's `protocol` field is read off this, so a mismatch would
      // label a row with an arm that did not run it.
      expect(protocol?.arm, arm).toBe(arm)
    }
  })
})
