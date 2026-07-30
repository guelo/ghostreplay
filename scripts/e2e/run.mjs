#!/usr/bin/env node
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  databasePathFor,
  newRunToken,
  removeDatabase,
  sweepAbandonedDatabases,
} from './run-db.mjs'
import { acquireRunSlot } from './run-slot.mjs'

/**
 * `playwright test` wrapper that gives each run its own ports and database.
 *
 * The allocation happens HERE rather than in playwright.config.ts because
 * Playwright re-evaluates that config in every worker process. Choosing a slot
 * there would either pick a different one per worker or depend on subtle
 * assumptions about env inheritance across forks. A parent process that decides
 * once and exports the result has neither problem: the config, the workers and
 * both web servers all read the same variables. See g-e2e-port-collide.
 */

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')
const playwrightBin = path.join(repoRoot, 'node_modules', '.bin', 'playwright')

/**
 * Ports and database. Any one of these means the caller has chosen the endpoints
 * deliberately — a debugging session, or the documented manual escape hatch — so
 * pass straight through rather than silently ignoring what they asked for.
 *
 * All or nothing, because these five are interdependent: allocating a slot and
 * filling in only the ones the caller left unset is how you end up with the
 * backend on the slot's port and the login fixture on the caller's, which is the
 * ECONNREFUSED failure g-e2e-port-collide was filed about.
 */
const ENDPOINT_VARS = [
  'E2E_FRONTEND_PORT',
  'E2E_BACKEND_PORT',
  'E2E_BASE_URL',
  'E2E_API_URL',
  'E2E_DATABASE_URL',
]

/**
 * Where artifacts land. Independent of the endpoints — wanting the report in a
 * particular directory says nothing about wanting to share a database with
 * whatever else is running — so these are honoured without switching isolation
 * off. They are the caller's problem to keep unique if two runs overlap.
 */
const ARTIFACT_VARS = ['E2E_OUTPUT_DIR', 'E2E_REPORT_DIR']

const runPlaywright = (env, inheritFds = []) =>
  new Promise((resolve) => {
    const child = spawn(playwrightBin, ['test', ...process.argv.slice(2)], {
      cwd: repoRoot,
      env,
      stdio: ['inherit', 'inherit', 'inherit', ...inheritFds],
    })

    // Forward interrupts so Ctrl-C tears down Playwright (and its web servers)
    // before this process exits and releases the slot.
    const forward = (signal) => () => {
      if (child.exitCode === null) child.kill(signal)
    }
    const onSigint = forward('SIGINT')
    const onSigterm = forward('SIGTERM')
    process.on('SIGINT', onSigint)
    process.on('SIGTERM', onSigterm)

    const finish = (code) => {
      process.off('SIGINT', onSigint)
      process.off('SIGTERM', onSigterm)
      resolve(code)
    }

    child.on('error', (error) => {
      console.error(`[e2e] could not start ${playwrightBin}: ${error.message}`)
      finish(1)
    })
    child.on('exit', (code, signal) => {
      finish(signal ? 128 + (os.constants.signals[signal] ?? 0) : (code ?? 1))
    })
  })

const main = async () => {
  const callerEndpoints = ENDPOINT_VARS.filter((name) => process.env[name])
  if (callerEndpoints.length > 0) {
    console.log(
      `[e2e] ${callerEndpoints.join(', ')} set by caller; skipping per-run isolation`,
    )
    return runPlaywright(process.env)
  }

  const slot = await acquireRunSlot()

  // Named for this run, not for the slot, so no surviving process from an
  // earlier run can be sharing it; see run-db.mjs.
  const tmpDir = path.join(repoRoot, 'backend', '.tmp')
  const dbPath = databasePathFor(tmpDir, slot.slot, newRunToken())

  fs.mkdirSync(tmpDir, { recursive: true })
  const swept = await sweepAbandonedDatabases(tmpDir, { heldSlot: slot.slot })
  if (swept.length > 0) {
    console.log(`[e2e] removed ${swept.length} database(s) left by runs that are gone`)
  }

  let released = false
  const release = () => {
    if (released) return
    released = true
    removeDatabase(dbPath)
    slot.release()
  }
  process.on('exit', release)

  const outputDir = process.env.E2E_OUTPUT_DIR ?? path.join('test-results', `slot-${slot.slot}`)
  const reportDir =
    process.env.E2E_REPORT_DIR ?? path.join('playwright-report', `slot-${slot.slot}`)

  const callerArtifacts = ARTIFACT_VARS.filter((name) => process.env[name])
  if (callerArtifacts.length > 0) {
    console.log(
      `[e2e] ${callerArtifacts.join(', ')} set by caller; those artifacts are not per-slot`,
    )
  }

  console.log(
    `[e2e] slot ${slot.slot}: frontend ${slot.frontendPort}, backend ${slot.backendPort}, ` +
      `db ${path.relative(repoRoot, dbPath)}\n` +
      `[e2e] report: npx playwright show-report ${reportDir}`,
  )

  // Hand the reservation to Playwright so the slot outlives an abruptly killed
  // runner; see reservationFd in run-slot.mjs for why that matters.
  const inheritFds = slot.reservationFd === null ? [] : [slot.reservationFd]
  if (inheritFds.length === 0) {
    console.log(
      '[e2e] no reservation descriptor to inherit; killing this runner outright ' +
        'frees its ports while Playwright is still using them, which shows up as ' +
        'a bind failure in the next run rather than as anything silent',
    )
  }

  try {
    return await runPlaywright(
      {
        ...process.env,
        E2E_FRONTEND_PORT: String(slot.frontendPort),
        E2E_BACKEND_PORT: String(slot.backendPort),
        E2E_BASE_URL: `http://127.0.0.1:${slot.frontendPort}`,
        E2E_API_URL: `http://127.0.0.1:${slot.backendPort}`,
        E2E_DATABASE_URL: `sqlite:///${dbPath.split(path.sep).join('/')}`,
        E2E_OUTPUT_DIR: outputDir,
        E2E_REPORT_DIR: reportDir,
      },
      inheritFds,
    )
  } finally {
    release()
  }
}

try {
  process.exitCode = await main()
} catch (error) {
  // Slot exhaustion carries instructions; a stack trace would bury them.
  console.error(`[e2e] ${error instanceof Error ? error.message : error}`)
  process.exitCode = 1
}
