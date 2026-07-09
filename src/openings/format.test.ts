import { describe, expect, it } from 'vitest'
import {
  buildMoveListTokens,
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

// Grade/tone boundaries re-centred onto the readiness-fold score distribution
// (g-xnv7, 2026-07-09 final grid: pooled p50≈10, p75≈21, p95≈44). These tests
// pin the boundaries so any future shift is a deliberate, reviewed change.
describe('getPriorityLabel', () => {
  it('grades at the re-centred boundaries (F<2, D 2–8, C 8–29, B 29–44, A≥44)', () => {
    expect(getPriorityLabel(null)).toBe('No Data')

    expect(getPriorityLabel(0)).toBe('F')
    expect(getPriorityLabel(1.9)).toBe('F')

    expect(getPriorityLabel(2)).toBe('D')
    expect(getPriorityLabel(7.9)).toBe('D')

    expect(getPriorityLabel(8)).toBe('C')
    expect(getPriorityLabel(28.9)).toBe('C')

    expect(getPriorityLabel(29)).toBe('B')
    expect(getPriorityLabel(43.9)).toBe('B')

    expect(getPriorityLabel(44)).toBe('A')
    expect(getPriorityLabel(100)).toBe('A')
  })
})

describe('getPriorityTone', () => {
  it('maps tones to the re-centred boundaries (alert<5, watch 5–29, steady≥29)', () => {
    expect(getPriorityTone(null)).toBe('muted')

    expect(getPriorityTone(0)).toBe('alert')
    expect(getPriorityTone(4.9)).toBe('alert')

    expect(getPriorityTone(5)).toBe('watch')
    expect(getPriorityTone(28.9)).toBe('watch')

    expect(getPriorityTone(29)).toBe('steady')
    expect(getPriorityTone(100)).toBe('steady')
  })
})

describe('numeric formatters', () => {
  it('formatScore rounds and dashes nulls', () => {
    expect(formatScore(null)).toBe('—')
    expect(formatScore(0)).toBe('0')
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
    expect(getGradeToken(1.9)).toBe('f')
    expect(getGradeToken(2)).toBe('d')
    expect(getGradeToken(8)).toBe('c')
    expect(getGradeToken(29)).toBe('b')
    expect(getGradeToken(44)).toBe('a')
  })

  it('getGradeText spells out each grade and uses sentence-case "No data" for null', () => {
    expect(getGradeText(90)).toBe('Grade A')
    expect(getGradeText(42)).toBe('Grade B')
    expect(getGradeText(20)).toBe('Grade C')
    expect(getGradeText(4)).toBe('Grade D')
    expect(getGradeText(1)).toBe('Grade F')

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

describe('buildMoveListTokens', () => {
  it('numbers White plies and bares Black plies, flagging the last', () => {
    const tokens = buildMoveListTokens(['e4', 'c6', 'Bc4'], 1)
    expect(tokens).toEqual([
      { text: '1.e4', isLast: false },
      { text: 'c6', isLast: false },
      { text: '2.Bc4', isLast: true },
    ])
  })

  it('honors a non-1 startPly (drill starting mid-game)', () => {
    // startPly 4 = Black's move 2, so the first token is a bare Black SAN and the
    // next is White's move 3.
    const tokens = buildMoveListTokens(['Nc6', 'Bb5'], 4)
    expect(tokens.map((t) => t.text)).toEqual(['Nc6', '3.Bb5'])
  })

  it('short-circuits empty input regardless of startPly', () => {
    expect(buildMoveListTokens([], 1)).toEqual([])
    expect(buildMoveListTokens([], 7)).toEqual([])
    expect(buildMoveListTokens([], null)).toEqual([])
  })

  it('treats a null/0 startPly as White move 1', () => {
    expect(buildMoveListTokens(['e4'], null)[0].text).toBe('1.e4')
    expect(buildMoveListTokens(['e4'], 0)[0].text).toBe('1.e4')
  })
})
