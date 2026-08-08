/**
 * The page's controls, and the mapping from them to a run configuration.
 *
 * Split out of `main.ts` so this seam can be exercised against the real
 * `bench/device/index.html`: it is where a browser's input handling meets the
 * run's controls, and it has been wrong twice. First by reading fields as
 * `Number(value) || fallback`, so a typo became a plausible default. Then by
 * trusting `<input type="number">` to hand back what the operator typed — it
 * runs the HTML value-sanitization algorithm, so `e` reads back as the empty
 * string and the field looks untouched. Both failures ran a different protocol
 * than the operator asked for and recorded it as though they had asked.
 *
 * Nothing here touches the global `document`, so a test can hand it the parsed
 * page and get the same mapping the page itself uses.
 */

import type { BenchPositionSetId } from './positions'
import type { BenchRunConfig } from './config'
import { typedNumberField } from './config'

/** Controls whose `value` is read and restored verbatim. */
export const VALUE_CONTROL_IDS = [
  'deviceLabel',
  'notes',
  'positionSetId',
  'thermalPlies',
  'repeats',
  'mode',
  'depth',
  'blockCooldownMs',
] as const

export type ValueControlId = (typeof VALUE_CONTROL_IDS)[number]

export type BenchFormControls = {
  deviceLabel: HTMLInputElement
  notes: HTMLInputElement
  positionSetId: HTMLSelectElement
  thermalPlies: HTMLInputElement
  repeats: HTMLInputElement
  mode: HTMLSelectElement
  depth: HTMLInputElement
  blockCooldownMs: HTMLInputElement
  warmup: HTMLInputElement
}

const required = <T extends Element>(root: ParentNode, selector: string): T => {
  const element = root.querySelector<T>(selector)
  if (!element) {
    // Louder than the `as` cast this replaces, which produced null and failed
    // later as an unrelated TypeError.
    throw new Error(`the bench page is missing its ${selector} control`)
  }
  return element
}

export const benchFormControls = (root: ParentNode): BenchFormControls => ({
  deviceLabel: required(root, '#deviceLabel'),
  notes: required(root, '#notes'),
  positionSetId: required(root, '#positionSetId'),
  thermalPlies: required(root, '#thermalPlies'),
  repeats: required(root, '#repeats'),
  mode: required(root, '#mode'),
  depth: required(root, '#depth'),
  blockCooldownMs: required(root, '#blockCooldownMs'),
  warmup: required(root, '#warmup'),
})

/**
 * The selected set, AS SELECTED — never narrowed to a known id here.
 *
 * This used to be `value === 'smoke-6' ? 'smoke-6' : 'thermal-40'`, a two-way
 * test that answered every OTHER value with the thermal sequence. With a third
 * set that stops being a harmless simplification: selecting `best-30` would have
 * run 40 thermal positions while the run header recorded `best-30`, which is the
 * silent substitution this module's own docstring exists to prevent.
 *
 * `configProblems` refuses an id outside `BENCH_POSITION_SET_IDS` before a
 * single measurement runs. This function's only job is to not destroy the
 * evidence that there is something to refuse — the same contract `typedNumber`
 * has for numbers.
 */
const selectedPositionSetId = (control: { value: string }): BenchPositionSetId =>
  control.value as BenchPositionSetId

/**
 * Every number goes through `typedNumberField`, never `Number(x) || fallback`:
 * an unreadable or out-of-range entry has to reach `configProblems` intact to be
 * refused. Substituting a plausible default here would be a silent answer to a
 * question the operator got wrong.
 */
export const readConfig = (controls: BenchFormControls): BenchRunConfig => ({
  deviceLabel: controls.deviceLabel.value.trim(),
  notes: controls.notes.value.trim(),
  mode: controls.mode.value === 'cold' ? 'cold' : 'sequence',
  positionSetId: selectedPositionSetId(controls.positionSetId),
  thermalPlies: typedNumberField(controls.thermalPlies),
  // `?? NaN` rather than `?? 1`: a cleared field is a mistake to report, not a
  // silent one-repeat run that §10.4 would then merely warn about.
  repeats: typedNumberField(controls.repeats) ?? NaN,
  warmup: controls.warmup.checked,
  blockCooldownMs: typedNumberField(controls.blockCooldownMs),
  depth: typedNumberField(controls.depth),
})
