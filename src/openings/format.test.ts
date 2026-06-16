import { describe, expect, it } from 'vitest'
import {
  formatGames,
  formatMoveLabel,
  formatOpeningName,
  formatPercent,
  formatScore,
  formatTerminalReason,
  getGradeText,
  getGradeToken,
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

describe('tree-card helpers', () => {
  it('getGradeToken tracks the getPriorityLabel boundaries', () => {
    expect(getGradeToken(null)).toBe('none')
    expect(getGradeToken(44.9)).toBe('f')
    expect(getGradeToken(45)).toBe('d')
    expect(getGradeToken(55)).toBe('c')
    expect(getGradeToken(70)).toBe('b')
    expect(getGradeToken(85)).toBe('a')
  })

  it('getGradeText spells out each grade and uses sentence-case "No data" for null', () => {
    expect(getGradeText(90)).toBe('Grade A')
    expect(getGradeText(72)).toBe('Grade B')
    expect(getGradeText(60)).toBe('Grade C')
    expect(getGradeText(48)).toBe('Grade D')
    expect(getGradeText(20)).toBe('Grade F')

    // Deliberate casing split: the accessible grade name is sentence case while
    // the visible label stays title case. Pinned together so neither drifts.
    expect(getGradeText(null)).toBe('No data')
    expect(getPriorityLabel(null)).toBe('No Data')
  })

  it('formatMoveLabel labels the root and alternating colours', () => {
    expect(formatMoveLabel(0, null)).toBe('Starting position')
    expect(formatMoveLabel(1, 'e4')).toBe('1. e4')
    expect(formatMoveLabel(2, 'e5')).toBe('1… e5')
    expect(formatMoveLabel(3, 'Nf3')).toBe('2. Nf3')
  })

  it('formatTerminalReason maps each code and falls back to "End of line"', () => {
    expect(formatTerminalReason('checkmate')).toBe('Checkmate')
    expect(formatTerminalReason('stalemate')).toBe('Stalemate')
    expect(formatTerminalReason('opening_boundary')).toBe('Opening boundary reached')
    expect(formatTerminalReason('no_children')).toBe('End of line')
    expect(formatTerminalReason(null)).toBe('End of line')
  })

  it('formatOpeningName passes through names and defaults null to "Unclassified"', () => {
    expect(formatOpeningName('Sicilian Defense')).toBe('Sicilian Defense')
    expect(formatOpeningName(null)).toBe('Unclassified')
  })
})
