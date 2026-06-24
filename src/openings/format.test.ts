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

// Grade/tone boundaries re-centred onto the observed v2 distribution (g-g5sg,
// 2026-06-24 calibration: pooled p50≈33, p95≈55, six included pairs all median
// 31–34). The original A≥85…F<45 scale graded ~95% of cards F/alert; the bands
// now track pooled percentiles. These tests pin the new boundaries so any future
// shift is a deliberate, reviewed change.
describe('getPriorityLabel', () => {
  it('grades at the re-centred boundaries (F<22, D 22–28, C 28–38, B 38–50, A≥50)', () => {
    expect(getPriorityLabel(null)).toBe('No Data')

    expect(getPriorityLabel(0)).toBe('F')
    expect(getPriorityLabel(21.9)).toBe('F')

    expect(getPriorityLabel(22)).toBe('D')
    expect(getPriorityLabel(27.9)).toBe('D')

    expect(getPriorityLabel(28)).toBe('C')
    expect(getPriorityLabel(37.9)).toBe('C')

    expect(getPriorityLabel(38)).toBe('B')
    expect(getPriorityLabel(49.9)).toBe('B')

    expect(getPriorityLabel(50)).toBe('A')
    expect(getPriorityLabel(100)).toBe('A')
  })
})

describe('getPriorityTone', () => {
  it('maps tones to the re-centred boundaries (alert<25, watch 25–38, steady≥38)', () => {
    expect(getPriorityTone(null)).toBe('muted')

    expect(getPriorityTone(0)).toBe('alert')
    expect(getPriorityTone(24.9)).toBe('alert')

    expect(getPriorityTone(25)).toBe('watch')
    expect(getPriorityTone(37.9)).toBe('watch')

    expect(getPriorityTone(38)).toBe('steady')
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
    expect(getGradeToken(21.9)).toBe('f')
    expect(getGradeToken(22)).toBe('d')
    expect(getGradeToken(28)).toBe('c')
    expect(getGradeToken(38)).toBe('b')
    expect(getGradeToken(50)).toBe('a')
  })

  it('getGradeText spells out each grade and uses sentence-case "No data" for null', () => {
    expect(getGradeText(90)).toBe('Grade A')
    expect(getGradeText(42)).toBe('Grade B')
    expect(getGradeText(32)).toBe('Grade C')
    expect(getGradeText(24)).toBe('Grade D')
    expect(getGradeText(10)).toBe('Grade F')

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
