import path from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * The one place the e2e stack resolves its endpoints and database.
 *
 * Playwright loads `playwright.config.ts` in the main process to start the web
 * servers, and again in every worker process to read project settings; the spec
 * files and fixtures then run inside those workers. Every one of those readers
 * needs the same backend URL, so each of them used to carry its own
 * `process.env.E2E_API_URL ?? 'http://127.0.0.1:8010'`. That drifted: overriding
 * E2E_BACKEND_PORT alone started the backend on the new port while all four
 * copies of the default still logged in against 8010, and 28 of 29 tests failed
 * with ECONNREFUSED (g-e2e-port-collide). Import from here instead of writing a
 * default inline — a default that exists twice is a default that will disagree.
 *
 * `scripts/e2e/run.mjs` sets these variables to a per-run port pair and database
 * so concurrent runs cannot collide. The fixed fallbacks below only apply when
 * `playwright test` is invoked directly, outside that runner.
 */

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

export const frontendPort = Number(process.env.E2E_FRONTEND_PORT ?? 4173)
export const backendPort = Number(process.env.E2E_BACKEND_PORT ?? 8010)

export const baseURL = process.env.E2E_BASE_URL ?? `http://127.0.0.1:${frontendPort}`
export const apiURL = process.env.E2E_API_URL ?? `http://127.0.0.1:${backendPort}`

const defaultDbPath = path.resolve(repoRoot, 'backend', '.tmp', 'e2e.sqlite3')
// SQLAlchemy URLs are '/'-separated regardless of the host path separator.
const toSqliteUrl = (absolutePath: string) =>
  `sqlite:///${absolutePath.split(path.sep).join('/')}`

export const databaseUrl = process.env.E2E_DATABASE_URL ?? toSqliteUrl(defaultDbPath)

/**
 * Artifact directories, isolated for the same reason the ports are.
 *
 * Playwright empties `outputDir` when a run starts, so a second run deletes the
 * traces and videos the first is still writing — and the attachment paths it
 * already printed then point at nothing. Reports are worse: the loser's is
 * simply overwritten by the winner.
 */
export const outputDir = process.env.E2E_OUTPUT_DIR ?? 'test-results'
export const reportDir = process.env.E2E_REPORT_DIR ?? 'playwright-report'
