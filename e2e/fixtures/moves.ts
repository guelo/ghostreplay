import { expect, type Locator } from '@playwright/test'

/**
 * How long to wait for the move list to reach a given length.
 *
 * Every caller is really waiting on the same thing: the opponent's reply, which
 * is a backend round trip through Maia inference. The 5s global expect timeout
 * covers that comfortably on an idle machine — the suite runs in ~26s — and not
 * at all on a busy one. Once per-run port and database isolation let two agents
 * run e2e at the same time (g-e2e-port-collide), suite wall-clock roughly
 * doubles and these polls are the first thing to fall over: four of them failed
 * across a concurrent pair, every one with "Timeout 5000ms exceeded" on a move
 * count of 3-instead-of-4. The bead records the same signature failing an actual
 * pre-push gate for a backend-only change that could not render a move row.
 *
 * A longer budget costs a passing run nothing — expect.poll returns the moment
 * the predicate holds — and a genuine "moves never render" regression still
 * fails, just later. Trading slower reporting on a broken build for no false
 * failures on a healthy one is the right way round for a gate.
 *
 * Kept under half the 60s per-test timeout so a test that waits twice still
 * fails as a clean assertion rather than an uninformative test timeout.
 */
export const MOVE_LIST_TIMEOUT_MS = 25_000

/**
 * Wait until `moves` matches at least `minimum` elements.
 *
 * Takes the locator because the layouts genuinely differ — the vertical list
 * renders `.move-list-grid .move-button`, the mobile strip renders `.h-move` —
 * but the timeout must not. Four specs had grown their own copy of this poll,
 * and a timeout that exists four times is a timeout that gets fixed once.
 */
export const waitForMoveCountAtLeast = async (
  moves: Locator,
  minimum: number,
): Promise<void> => {
  await expect
    .poll(async () => moves.count(), { timeout: MOVE_LIST_TIMEOUT_MS })
    .toBeGreaterThanOrEqual(minimum)
}
