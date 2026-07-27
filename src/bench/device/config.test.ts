import { describe, expect, it } from 'vitest'
import { configProblems, typedNumber, typedNumberField } from './config'
import type { BenchRunConfig } from './config'
import { planWarnings } from '../method'

const valid: BenchRunConfig = {
  deviceLabel: 'MacBook Pro M1, macOS 15, Chromium',
  notes: '',
  mode: 'sequence',
  positionSetId: 'thermal-40',
  repeats: 3,
  arms: ['current'],
  blockCooldownMs: 60_000,
}

describe('configProblems', () => {
  it('accepts the standard control run', () => {
    expect(configProblems(valid)).toEqual([])
  })

  it('refuses a number that arrived as NaN, which would apply as no control at all', () => {
    // The failure this exists for: `--cooldown nope` → NaN → `NaN > 0` is false,
    // so the runner neither sleeps nor warns, and JSON.stringify writes the plan
    // field as `null`. A thermal ramp reported as a method-valid run.
    expect(configProblems({ ...valid, blockCooldownMs: NaN }).join(' ')).toMatch(
      /blockCooldownMs must be a whole number/,
    )
    expect(configProblems({ ...valid, repeats: NaN }).join(' ')).toMatch(/repeats/)
    expect(configProblems({ ...valid, depth: NaN }).join(' ')).toMatch(/depth/)
  })

  it('shows that a NaN cooldown really would pass the method check unnoticed', () => {
    // Pinning the reason the refusal is at the boundary rather than a warning:
    // every comparison in `planWarnings` is false for NaN, so nothing downstream
    // can catch it.
    const warnings = planWarnings({
      repeats: 3,
      armCount: 1,
      blockCount: 3,
      armOrderBalanced: true,
      blockCooldownMs: NaN,
      thermalPlies: 40,
      build: 'bundled',
      requestedDepth: 17,
      sessionDepth: 17,
      source: { gitRevision: 'a'.repeat(40), gitDirty: false, workerBundleFile: null, workerBundleSha256: null },
    })

    expect(warnings).toEqual([])
  })

  it('refuses a mode or position set it would silently substitute for', () => {
    // `planBlocks` treats anything but `cold` as `sequence`, and
    // `buildPositionSet` anything but `smoke-6` as the thermal sequence — so an
    // unknown value runs a different plan than the header records.
    expect(configProblems({ ...valid, mode: 'sequince' as BenchRunConfig['mode'] }).join(' ')).toMatch(
      /mode must be one of sequence, cold/,
    )
    expect(
      configProblems({
        ...valid,
        positionSetId: 'smoke6' as BenchRunConfig['positionSetId'],
      }).join(' '),
    ).toMatch(/positionSetId must be one of/)
  })

  it('refuses unknown, missing, or repeated arms', () => {
    expect(configProblems({ ...valid, arms: [] }).join(' ')).toMatch(/at least one arm/)
    expect(
      configProblems({ ...valid, arms: ['variantC' as BenchRunConfig['arms'][number]] }).join(' '),
    ).toMatch(/unknown arm/)
    // A repeat doubles that arm's blocks while `armOrderBalanced` still calls the
    // rotation balanced.
    expect(configProblems({ ...valid, arms: ['current', 'current'] }).join(' ')).toMatch(
      /arms must be unique/,
    )
  })

  it('refuses more thermal plies than the stored game has, instead of capping silently', () => {
    // `buildThermalPositions` caps at the game's length, so `--plies 500` would
    // measure 60 and the file would say nothing about the substitution.
    expect(configProblems({ ...valid, thermalPlies: 500 }).join(' ')).toMatch(
      /thermalPlies must be between 1 and 60/,
    )
    expect(configProblems({ ...valid, thermalPlies: 60 })).toEqual([])
  })

  it('refuses a warm-up in cold mode, which the schedule would drop but the header would claim', () => {
    // Every cold measurement already gets a fresh worker, so `planBlocks` ignores
    // the flag — while `plan.warmup: true` went into the run header, describing a
    // priming row the file does not contain.
    expect(configProblems({ ...valid, mode: 'cold', warmup: true }).join(' ')).toMatch(
      /warmup cannot be combined with mode=cold/,
    )
    expect(configProblems({ ...valid, mode: 'cold', warmup: false })).toEqual([])
  })

  it('refuses a run that cannot name its hardware', () => {
    expect(configProblems({ ...valid, deviceLabel: '  ' }).join(' ')).toMatch(/deviceLabel is required/)
  })

  it('refuses a fractional or out-of-range value', () => {
    expect(configProblems({ ...valid, repeats: 0 }).join(' ')).toMatch(/repeats/)
    expect(configProblems({ ...valid, blockCooldownMs: 60_000.5 }).join(' ')).toMatch(/whole number/)
    expect(configProblems({ ...valid, depth: 170 }).join(' ')).toMatch(/depth must be between/)
  })

  it('reports every problem at once, not one per attempt', () => {
    expect(configProblems({ ...valid, repeats: NaN, depth: 0, arms: [] })).toHaveLength(3)
  })

  it('names an unreadable number instead of calling it null', () => {
    // `JSON.stringify(NaN)` is the string "null", which would report a typed
    // `nope` as a missing field.
    expect(configProblems({ ...valid, repeats: NaN }).join(' ')).toMatch(/got NaN/)
  })
})

describe('typedNumberField', () => {
  it('refuses an entry the browser sanitized away, which reads as blank', () => {
    // `<input type="number">` discards `e` before any of our code runs: `.value`
    // is '' and only `validity.badInput` says the field is not empty. The page's
    // controls are text + inputmode so this cannot arise (see form.test.ts), and
    // this check is what stops a control switched back to `type="number"` from
    // quietly restoring the default-on-typo behaviour. jsdom sanitizes the value
    // but always reports badInput: false, so the flag is set explicitly here.
    expect(typedNumberField({ value: '', validity: { badInput: true } })).toBeNaN()
    expect(typedNumberField({ value: '', validity: { badInput: false } })).toBeUndefined()
    expect(typedNumberField({ value: '40', validity: { badInput: false } })).toBe(40)
  })
})

describe('typedNumber', () => {
  it('keeps an unreadable entry unreadable so the guard can refuse it', () => {
    // The failure this exists for: the page read its number fields as
    // `Number(value) || fallback`, so a typed 0 became the default 40 plies and
    // `nope` became 3 repeats — a substitution the run header then recorded as
    // the requested value. HTML min/max does not catch it either: the page
    // starts from a `type="button"` and never checks form validity.
    expect(typedNumber('nope')).toBeNaN()
    expect(typedNumber('0')).toBe(0)
    expect(configProblems({ ...valid, thermalPlies: typedNumber('0') }).join(' ')).toMatch(
      /thermalPlies must be between 1 and 60/,
    )
    expect(configProblems({ ...valid, blockCooldownMs: typedNumber('nope') }).join(' ')).toMatch(
      /blockCooldownMs must be a whole number/,
    )
  })

  it('treats a blank field as unset, which is what a default is for', () => {
    expect(typedNumber('')).toBeUndefined()
    expect(typedNumber('   ')).toBeUndefined()
    expect(configProblems({ ...valid, depth: typedNumber(''), thermalPlies: typedNumber('') })).toEqual([])
  })

  it('reads an ordinary entry as the number it is', () => {
    expect(typedNumber('60000')).toBe(60_000)
    expect(typedNumber(' 40 ')).toBe(40)
  })
})
