import { describe, expect, it } from 'vitest'
import { compareRootScores } from './compareRootScores'
import type { EngineScore } from './stockfishMessages'

const cp = (value: number): EngineScore => ({ type: 'cp', value })
const mate = (value: number): EngineScore => ({ type: 'mate', value })

describe('compareRootScores', () => {
  describe('category ordering: losing mate < cp < winning mate', () => {
    it('ranks a winning mate above any centipawn score', () => {
      expect(compareRootScores(mate(10), cp(9990))).toBe(1)
      expect(compareRootScores(cp(9990), mate(10))).toBe(-1)
    })

    it('ranks a losing mate below any centipawn score', () => {
      expect(compareRootScores(mate(-10), cp(-9990))).toBe(-1)
      expect(compareRootScores(cp(-9990), mate(-10))).toBe(1)
    })

    it('ranks a winning mate above a losing mate in both argument orders', () => {
      expect(compareRootScores(mate(8), mate(-1))).toBe(1)
      expect(compareRootScores(mate(-1), mate(8))).toBe(-1)
    })
  })

  describe('within a category', () => {
    it('prefers the shorter winning mate', () => {
      expect(compareRootScores(mate(2), mate(5))).toBe(1)
      expect(compareRootScores(mate(5), mate(2))).toBe(-1)
    })

    it('prefers the longer losing mate', () => {
      expect(compareRootScores(mate(-10), mate(-1))).toBe(1)
      expect(compareRootScores(mate(-1), mate(-10))).toBe(-1)
    })

    it('prefers the larger centipawn score', () => {
      expect(compareRootScores(cp(30), cp(10))).toBe(1)
      expect(compareRootScores(cp(-300), cp(-10))).toBe(-1)
    })

    it('reports identical values as equal', () => {
      expect(compareRootScores(cp(0), cp(0))).toBe(0)
      expect(compareRootScores(mate(4), mate(4))).toBe(0)
      expect(compareRootScores(mate(-4), mate(-4))).toBe(0)
    })
  })

  it('separates pairs that win-chance conversion collapses', () => {
    // calculateWinChance clamps both of these to the same ceiling; typed ordering
    // must still prefer the faster mate, which is why §5.4 forbids it.
    expect(compareRootScores(mate(1), mate(30))).toBe(1)
    expect(compareRootScores(cp(9000), cp(20000))).toBe(-1)
  })

  it('prefers a mate over the CP surrogate that would outrank it', () => {
    // §5.5's representation conflict: mate +5 maps to a finite 9960 surrogate,
    // which loses to a genuine cp 9990 under arithmetic but not under ordering.
    expect(compareRootScores(mate(5), cp(9990))).toBe(1)
  })

  describe('order properties', () => {
    const scores: EngineScore[] = [
      mate(-1),
      mate(-3),
      mate(-30),
      cp(-9990),
      cp(-30),
      cp(0),
      cp(30),
      cp(9990),
      mate(30),
      mate(3),
      mate(1),
    ]

    it('is total: every pair compares to exactly one of -1, 0, 1', () => {
      for (const a of scores) {
        for (const b of scores) {
          expect([-1, 0, 1]).toContain(compareRootScores(a, b))
        }
      }
    })

    it('is antisymmetric', () => {
      for (const a of scores) {
        for (const b of scores) {
          expect(compareRootScores(a, b) + compareRootScores(b, a)).toBe(0)
        }
      }
    })

    it('is transitive', () => {
      for (const a of scores) {
        for (const b of scores) {
          for (const c of scores) {
            if (compareRootScores(a, b) >= 0 && compareRootScores(b, c) >= 0) {
              expect(compareRootScores(a, c)).toBeGreaterThanOrEqual(0)
            }
          }
        }
      }
    })

    it('agrees with the declared best-to-worst ordering', () => {
      // `scores` is listed worst-first; sorting by the comparator must reproduce it.
      const shuffled = [...scores].reverse()
      const sorted = [...shuffled].sort((a, b) => compareRootScores(a, b))
      expect(sorted).toEqual(scores)
    })
  })
})
