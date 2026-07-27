import type { CachedAnalysis } from '../utils/api'

/**
 * Fixture bridge for `/api/analysis/lookup` responses (g-v21l).
 *
 * The wire is now THREE surfaces, not one: the generic read fields
 * (`position_trusted` / `move_trusted` / `best_*`), the DRILL_GRADE fields
 * (`drill_best_move_uci` / `position_eval_loss_cp`), and the publication surfaces
 * (`reusable_analysis` / `publication_best`). A CANONICAL row holds every
 * capability for every viewer, so for a canonical hit all three surfaces agree —
 * and that canonical-parity emission is exactly what the pre-g-v21l fixtures
 * describe.
 *
 * This derives the missing surfaces for such a fixture so the pre-existing suites
 * keep describing canonical behavior in their original shorthand. Any surface a
 * fixture states EXPLICITLY is left untouched, so a test can still describe a
 * refused pairing (`reusable_analysis: null`), a single-consumer approval, a
 * non-DRILL_GRADE row (`drill_best_move_uci: null`), or a read-only grant.
 */
export const withCanonicalParitySurfaces = (
  entry: Partial<CachedAnalysis>,
): Partial<CachedAnalysis> => {
  const out: Partial<CachedAnalysis> = { ...entry }

  // DRILL grain: a canonical position winner is also the drill winner.
  if (!('drill_best_move_uci' in entry)) {
    out.drill_best_move_uci =
      entry.position_trusted === true ? entry.best_move_uci ?? null : null
  }

  // Publication reconciliation: position-grain only, both consumers.
  if (!('publication_best' in entry)) {
    out.publication_best =
      entry.position_trusted === true && entry.best_move_uci != null
        ? {
            best_move_uci: entry.best_move_uci,
            interactive_analysis_reuse: true,
            game_analysis_reuse: true,
          }
        : null
  }

  // Atomic reuse tuple: emitted only when BOTH grains are trusted and the tuple
  // is complete — the same combination the old published gate required.
  if (!('reusable_analysis' in entry)) {
    const complete =
      entry.position_trusted === true &&
      entry.move_trusted === true &&
      entry.best_move_uci != null &&
      entry.eval_delta != null &&
      Number.isFinite(entry.eval_delta)
    out.reusable_analysis = complete
      ? {
          best_move_uci: entry.best_move_uci as string,
          best_line_uci: entry.best_line_uci ?? null,
          best_eval: entry.best_eval ?? null,
          best_eval_mate: entry.best_eval_mate ?? null,
          played_eval: entry.played_eval ?? null,
          played_eval_mate: entry.played_eval_mate ?? null,
          classification: entry.classification ?? null,
          eval_delta: entry.eval_delta as number,
          interactive_analysis_reuse: true,
          game_analysis_reuse: true,
        }
      : null
  }

  return out
}

/**
 * Apply {@link withCanonicalParitySurfaces} to every value of a lookup-result map.
 * Installed in the `../utils/api` mock so existing fixtures keep working; a test
 * that states a surface explicitly still wins.
 */
export const withLookupSurfaces = (results: unknown): unknown => {
  if (!(results instanceof Map)) return results
  return new Map(
    Array.from(results.entries()).map(([key, value]) => [
      key,
      value && typeof value === 'object'
        ? withCanonicalParitySurfaces(value as Partial<CachedAnalysis>)
        : value,
    ]),
  )
}
