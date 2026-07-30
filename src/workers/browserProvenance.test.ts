import { readFileSync, readdirSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { buildBrowserProvenance, workerTupleProvenance } from './browserProvenance'
import type { AnalysisWorkerResponse } from './analysisMessages'

type AnalysisResponse = Extract<AnalysisWorkerResponse, { type: 'analysis' }>

/** A complete legacy tuple as the worker emits it; each test states its honesty fields. */
const response = (over: Partial<AnalysisResponse> = {}): AnalysisResponse => ({
  type: 'analysis',
  id: 'req-1',
  move: 'e2e4',
  bestMove: 'd2d4',
  bestLine: ['d2d4'],
  bestEval: 40,
  playedEval: 10,
  bestEvalMate: null,
  playedEvalMate: null,
  delta: 30,
  classification: 'inaccuracy',
  canonical: true,
  capFired: false,
  stopReason: 'bestmove',
  reachedDepth: 17,
  evidenceEligible: true,
  protocol: 'legacy',
  ...over,
})

describe('workerTupleProvenance', () => {
  it('stamps the caller\'s depth on an evidence-eligible tuple', () => {
    expect(workerTupleProvenance(response(), 21)).toEqual(buildBrowserProvenance(21))
  })

  it('withholds the claim from an ineligible tuple', () => {
    expect(workerTupleProvenance(response({ evidenceEligible: false }), 17)).toBeNull()
  })

  it('withholds from the worker-emitted tuples that are ineligible', () => {
    // The worker owns the rule (`!capFired && canonical` on the legacy arm). A
    // consumer that re-read the conditions is how g-coord-noncanon-prov happened:
    // gating on `capFired` stamped the completed delta-band fallback below.
    const completedNonCanonical = response({
      capFired: false,
      canonical: false,
      evidenceEligible: false,
    })
    expect(workerTupleProvenance(completedNonCanonical, 17)).toBeNull()

    const truncated = response({
      capFired: true,
      stopReason: 'deadline',
      evidenceEligible: false,
    })
    expect(workerTupleProvenance(truncated, 17)).toBeNull()

    // Candidate arms hardcode ineligible even on a tuple that reads complete and
    // canonical, so the gate must not second-guess the flag in either direction.
    const candidate = response({ protocol: 'variantA', evidenceEligible: false })
    expect(workerTupleProvenance(candidate, 17)).toBeNull()
  })

  it('reads evidenceEligible ALONE, never the conditions behind it', () => {
    // Both fixtures below CONTRADICT themselves on purpose: no legacy-arm search
    // emits them, because the worker computes the flag from exactly these two
    // fields. They are the only shape of test that can tell "reads the flag" apart
    // from "re-derives the rule and happens to agree" — every self-consistent
    // tuple passes under either implementation.
    //
    // Trusting the flag over the conditions is the actual contract, not a
    // shortcut: what makes a tuple eligible is the WORKER's to change (v3
    // producer envelopes, g-grade-v3-wire), and a consumer that vetoes on
    // today's two conditions would silently withhold claims from a protocol it
    // has never heard of.
    const eligibleDespiteBothConditions = response({
      capFired: true,
      stopReason: 'deadline',
      canonical: false,
      evidenceEligible: true,
    })
    expect(workerTupleProvenance(eligibleDespiteBothConditions, 17)).toEqual(
      buildBrowserProvenance(17),
    )

    // The mirror image, and the one that fails against the shipped bug: this is
    // precisely the tuple `capFired ? null : buildBrowserProvenance(...)` stamped.
    const ineligibleDespiteBothConditions = response({
      capFired: false,
      canonical: true,
      evidenceEligible: false,
    })
    expect(workerTupleProvenance(ineligibleDespiteBothConditions, 17)).toBeNull()
  })
})

describe('the eligibility rule lives in exactly one place', () => {
  // Provenance labels BY OMISSION — absent means browser-game-v1, present means
  // browser-game-v2 with a depth claim — so a second construction site is a
  // second chance to mislabel silently. Checked structurally rather than
  // behaviourally: "no consumer can bypass the gate" is a claim about what the
  // source does not contain, and a behavioural test would pass just as well
  // against a bypass nobody exercised yet.
  const SRC = resolve(__dirname, '..')

  const stripComments = (source: string) =>
    source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')

  const productionSources = (dir: string): string[] =>
    readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = join(dir, entry.name)
      if (entry.isDirectory()) return productionSources(full)
      if (!/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) return []
      return [full]
    })

  // Every way to MAKE a claim, not just the current helper's name: checking only
  // for `buildBrowserProvenance` would wave through a second builder or an inlined
  // object literal, which mislabel exactly as silently.
  const CLAIM_CONSTRUCTION = [
    // Calling the one builder.
    'buildBrowserProvenance',
    // Assembling the shape by hand. `BrowserAnalysisProvenance` REQUIRES this
    // field, so no literal can omit the token — and if the literal spreads it in
    // from somewhere else, that module holds the token and is caught instead.
    'search_limit_type',
  ]

  it('constructs a provenance claim in exactly one production module', () => {
    const sites = productionSources(SRC)
      .filter((file) => {
        const source = stripComments(readFileSync(file, 'utf8'))
        return CLAIM_CONSTRUCTION.some((pattern) => source.includes(pattern))
      })
      .map((file) => relative(SRC, file))
      .sort()
    // `types/analysis.ts` only DECLARES the shape; `browserProvenance.ts` is the
    // sole module that fills one in. Adding a third entry here is the review
    // moment: a new construction site needs its own honesty argument.
    expect(sites).toEqual(['types/analysis.ts', 'workers/browserProvenance.ts'])
  })
  // Residual gap, deliberately not chased: `x as BrowserAnalysisProvenance` over
  // untyped data (a parsed cache row, say) builds a claim without naming a field.
  // That is a deliberate cast, not the forgotten-condition slip this guards.
})
