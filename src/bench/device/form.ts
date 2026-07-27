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

import type { BenchArm } from '../benchRecord'
import { BENCH_ARMS } from '../benchRecord'
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
  /**
   * Checkboxes, not a `<select>`: §10.4's counterbalanced protocol order is a
   * property of ONE run measuring both arms, alternating between them. A control
   * that can only name a single arm makes the comparison unavailable to the
   * operator running the phone by hand — which is the only way the mobile
   * numbers can be captured at all.
   */
  armBoxes: HTMLInputElement[]
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
  armBoxes: [...root.querySelectorAll<HTMLInputElement>('input[name="arm"]')],
})

/**
 * Fixed order, independent of click order, so `arms[0]` is not an accident — and
 * taken from the schema's own list, so a checkbox whose value is not a real arm
 * cannot reach the runner.
 */
export const selectedArms = (controls: BenchFormControls): BenchArm[] =>
  BENCH_ARMS.filter((arm) => controls.armBoxes.some((box) => box.value === arm && box.checked))

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
  positionSetId: controls.positionSetId.value === 'smoke-6' ? 'smoke-6' : 'thermal-40',
  thermalPlies: typedNumberField(controls.thermalPlies),
  // `?? NaN` rather than `?? 1`: a cleared field is a mistake to report, not a
  // silent one-repeat run that §10.4 would then merely warn about.
  repeats: typedNumberField(controls.repeats) ?? NaN,
  arms: selectedArms(controls),
  warmup: controls.warmup.checked,
  blockCooldownMs: typedNumberField(controls.blockCooldownMs),
  depth: typedNumberField(controls.depth),
})
