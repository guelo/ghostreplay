import { describe, expect, it } from 'vitest'
import {
  formatGames,
  formatPercent,
  formatScore,
  getPriorityLabel,
  getPriorityTone,
} from './format'

// Grade/tone boundaries are the retained A≥85…F<45 scale. The first populated v2
// calibration (g-m36y) showed a low-skewed distribution but was ~95% one user —
// too thin to re-centre — so the original boundaries stand. These tests pin them
// so any future shift is a deliberate, reviewed change.
describe('getPriorityLabel', () => {
  it('grades at the retained boundaries (F<45, D 45–55, C 55–70, B 70–85, A≥85)', () => {
    expect(getPriorityLabel(null)).toBe('No Data')

    expect(getPriorityLabel(0)).toBe('F')
    expect(getPriorityLabel(44.9)).toBe('F')

    expect(getPriorityLabel(45)).toBe('D')
    expect(getPriorityLabel(54.9)).toBe('D')

    expect(getPriorityLabel(55)).toBe('C')
    expect(getPriorityLabel(69.9)).toBe('C')

    expect(getPriorityLabel(70)).toBe('B')
    expect(getPriorityLabel(84.9)).toBe('B')

    expect(getPriorityLabel(85)).toBe('A')
    expect(getPriorityLabel(100)).toBe('A')
  })
})

describe('getPriorityTone', () => {
  it('maps tones to the retained boundaries (alert<45, watch 45–65, steady≥65)', () => {
    expect(getPriorityTone(null)).toBe('muted')

    expect(getPriorityTone(0)).toBe('alert')
    expect(getPriorityTone(44.9)).toBe('alert')

    expect(getPriorityTone(45)).toBe('watch')
    expect(getPriorityTone(64.9)).toBe('watch')

    expect(getPriorityTone(65)).toBe('steady')
    expect(getPriorityTone(100)).toBe('steady')
  })
})

describe('numeric formatters', () => {
  it('formatScore rounds and dashes nulls', () => {
    expect(formatScore(null)).toBe('—')
    expect(formatScore(33.27)).toBe('33')
    expect(formatScore(54.7)).toBe('55')
  })

  it('formatPercent normalizes fractions and clamps to 0–100', () => {
    expect(formatPercent(null)).toBe('—')
    expect(formatPercent(0.63)).toBe('63%')
    expect(formatPercent(63)).toBe('63%')
    expect(formatPercent(150)).toBe('100%')
    expect(formatPercent(-2)).toBe('0%')
  })

  it('formatGames localizes counts and dashes nulls', () => {
    expect(formatGames(null)).toBe('—')
    expect(formatGames(7995)).toBe((7995).toLocaleString())
  })
})
