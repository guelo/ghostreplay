import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { benchFormControls, readConfig } from './form'
import { configProblems, typedNumberField } from './config'

/**
 * The form read against the REAL page, not a hand-built fixture.
 *
 * The bug this exists for was invisible to a unit test of the parsing helper:
 * the helper refused `'nope'` correctly, but no `<input type="number">` will ever
 * hand it that string. A number input runs the HTML value-sanitization
 * algorithm, so typing `e` leaves `.value === ''` — the field reads as untouched,
 * and thermal plies, depth and the cooldown all fell back to their defaults on a
 * run the operator thought they had configured. The only way to catch that is to
 * read the controls the page actually ships.
 */
const PAGE = resolve(__dirname, '..', '..', '..', 'bench', 'device', 'index.html')

const parsePage = (): Document =>
  new DOMParser().parseFromString(readFileSync(PAGE, 'utf8'), 'text/html')

const NUMERIC_CONTROL_IDS = ['thermalPlies', 'repeats', 'depth', 'blockCooldownMs'] as const

describe('the bench page form', () => {
  it('has every control the shell binds', () => {
    // `benchFormControls` throws by name, so a control renamed in the markup
    // fails here rather than as a TypeError on the phone that was about to run.
    expect(() => benchFormControls(parsePage())).not.toThrow()
  })

  it('keeps what was typed into a numeric control, however unreadable', () => {
    const page = parsePage()
    for (const id of NUMERIC_CONTROL_IDS) {
      const input = page.querySelector<HTMLInputElement>(`#${id}`)!
      input.value = 'e'
      // A `type="number"` control fails this line: the entry is discarded before
      // any of our code sees it, leaving a field indistinguishable from blank.
      expect(input.value, `${id} discarded what was typed into it`).toBe('e')
      expect(typedNumberField(input)).toBeNaN()
    }
  })

  it('refuses the run instead of substituting a default it could not read', () => {
    const page = parsePage()
    const controls = benchFormControls(page)
    controls.deviceLabel.value = 'MacBook Pro M1, macOS 15, Chromium'
    for (const id of NUMERIC_CONTROL_IDS) controls[id].value = 'e'

    const problems = configProblems(readConfig(controls))

    // One per control: silence on any of them is a default applied as though it
    // had been requested.
    expect(problems).toHaveLength(NUMERIC_CONTROL_IDS.length)
    for (const id of NUMERIC_CONTROL_IDS) {
      expect(problems.join(' ')).toMatch(new RegExp(`${id} must be a whole number, got NaN`))
    }
  })

  it('reads the shipped defaults as a valid control run', () => {
    const controls = benchFormControls(parsePage())
    controls.deviceLabel.value = 'MacBook Pro M1, macOS 15, Chromium'

    const config = readConfig(controls)

    expect(config).toMatchObject({
      mode: 'sequence',
      positionSetId: 'thermal-40',
      thermalPlies: 40,
      repeats: 3,
      blockCooldownMs: 60_000,
      // Blank means unset, which is the documented way to ask for this device's
      // own session depth.
      depth: undefined,
      warmup: false,
    })
    expect(configProblems(config)).toEqual([])
  })

  it('refuses a cleared repeat count rather than quietly measuring once', () => {
    const controls = benchFormControls(parsePage())
    controls.deviceLabel.value = 'MacBook Pro M1, macOS 15, Chromium'
    controls.repeats.value = ''

    expect(configProblems(readConfig(controls)).join(' ')).toMatch(/repeats must be a whole number/)
  })

  it('routes best-30 through to the runner instead of substituting thermal-40', () => {
    // The page offers the set; a mapping that answered every non-`smoke-6`
    // selection with `thermal-40` would run 40 thermal positions while the
    // header recorded `best-30` — 40 rows of the wrong corpus, on a phone, that
    // nothing downstream could tell from the real thing.
    const page = parsePage()
    const options = [...page.querySelectorAll<HTMLOptionElement>('#positionSetId option')]
    expect(options.map((option) => option.value)).toContain('best-30')

    const controls = benchFormControls(page)
    controls.deviceLabel.value = 'iPhone XR, iOS 17.7, Safari'
    controls.positionSetId.value = 'best-30'

    const config = readConfig(controls)
    expect(config.positionSetId).toBe('best-30')
    expect(configProblems(config)).toEqual([])
  })

  it('refuses an unrecognized set id rather than running another one', () => {
    // The realistic way this happens: an option is added to the markup and the
    // schema's list is not updated with it. `readConfig` must pass the value
    // through INTACT so `configProblems` can refuse it — the two-way test it
    // replaced would have answered `thermal-40` and run 40 positions.
    const page = parsePage()
    const select = page.querySelector<HTMLSelectElement>('#positionSetId')!
    const drifted = page.createElement('option')
    drifted.value = 'best-300'
    select.append(drifted)

    const controls = benchFormControls(page)
    controls.deviceLabel.value = 'iPhone XR, iOS 17.7, Safari'
    controls.positionSetId.value = 'best-300'

    expect(readConfig(controls).positionSetId).toBe('best-300')
    expect(configProblems(readConfig(controls)).join(' ')).toMatch(/positionSetId must be one of/)
  })

})
