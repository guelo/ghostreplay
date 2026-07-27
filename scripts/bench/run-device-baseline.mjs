#!/usr/bin/env node
/**
 * Desktop-control baseline for the analysis device benchmark
 * (g-grade-device-runner; g-two-search-grade §10.4 "and desktop control").
 *
 * Builds the bench entry, serves it with `vite preview` (which sets the same
 * COOP/COEP headers as the app), drives the SAME page an operator would use on a
 * phone through `window.__ghostBench.run`, and writes the JSONL under
 * docs/analysis/.
 *
 * Why the built bundle and not the dev server: §10.1 requires the runner to load
 * the ACTUAL BUNDLED worker. `detectBuildMode()` records which one produced a
 * file, so a dev-server run can never be quoted as a device baseline by mistake.
 *
 * iPhone/Safari and Android/Chrome baselines are NOT produced here — no headless
 * driver can stand in for real mobile silicon and thermals. Run those by hand
 * from the same page (docs/analysis/README.md) and commit their JSONL beside
 * this one.
 *
 * Usage:
 *   npm run bench:baseline -- --label "MacBook Pro M1, macOS 15, Chromium" \
 *     [--set thermal-40] [--plies 40] [--repeats 3] [--mode sequence] \
 *     [--arms current,variantA] [--warmup] [--cooldown 60000 (default)] \
 *     [--depth 17] [--out docs/analysis/<file>.jsonl] [--port 4180] [--skip-build]
 */
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..')

const parseArgs = (argv) => {
  const args = {}
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index]
    if (!token.startsWith('--')) continue
    const key = token.slice(2)
    const next = argv[index + 1]
    if (next === undefined || next.startsWith('--')) {
      args[key] = true
    } else {
      args[key] = next
      index += 1
    }
  }
  return args
}

const args = parseArgs(process.argv.slice(2))

/**
 * Refuse a bad flag HERE, before the build.
 *
 * `Number()` turns `nope` into NaN and a valueless `--cooldown` into `true` →
 * `1`, and neither survives contact with the runner as an error: the page's
 * validity checks compare with `>` and `===`, which are simply false for NaN, so
 * the run would skip its cooldown and report a clean method. The runner refuses
 * both at its own boundary too (`src/bench/device/config.ts`); this exists so the
 * refusal costs a second rather than a four-minute build.
 *
 * The bounds below mirror that module's. They are restated rather than imported
 * because this is plain Node ESM and cannot load TypeScript — a drift here costs
 * a clearer error message, not a bad measurement, since the runner refuses
 * anything this lets through.
 */
const invalid = []
const intOption = (flag, raw, { min, max, fallback }) => {
  if (raw === undefined) return fallback
  if (typeof raw !== 'string') {
    invalid.push(`--${flag} needs a value`)
    return fallback
  }
  const value = Number(raw)
  if (!Number.isInteger(value) || value < min || value > max) {
    invalid.push(`--${flag} must be a whole number between ${min} and ${max}, got "${raw}"`)
    return fallback
  }
  return value
}
const enumOption = (flag, raw, allowed, fallback) => {
  if (raw === undefined) return fallback
  if (typeof raw !== 'string' || !allowed.includes(raw)) {
    invalid.push(`--${flag} must be one of ${allowed.join(', ')}, got ${JSON.stringify(raw)}`)
    return fallback
  }
  return raw
}

const label = typeof args.label === 'string' ? args.label : null
if (!label) {
  invalid.push('--label "<hardware, OS, browser>" is required: a run header without hardware is not evidence.')
}

const port = intOption('port', args.port, { min: 1, max: 65_535, fallback: 4180 })
const positionSetId = enumOption('set', args.set, ['smoke-6', 'thermal-40'], 'thermal-40')
// 60 is the stored game's length; `buildThermalPositions` caps rather than
// refuses, so asking for more would quietly measure 60.
const thermalPlies = intOption('plies', args.plies, { min: 1, max: 60, fallback: 40 })
const repeats = intOption('repeats', args.repeats, { min: 1, max: 1_000, fallback: 3 })
const mode = enumOption('mode', args.mode, ['sequence', 'cold'], 'sequence')
const depth = intOption('depth', args.depth, { min: 1, max: 30, fallback: undefined })
const KNOWN_ARMS = ['current', 'variantA', 'variantB']
const armsRaw = args.arms ?? args.arm ?? 'current'
const arms =
  typeof armsRaw === 'string'
    ? armsRaw.split(',').map((arm) => arm.trim()).filter((arm) => arm.length > 0)
    : []
for (const arm of arms) {
  if (!KNOWN_ARMS.includes(arm)) invalid.push(`--arms: unknown arm "${arm}" (known: ${KNOWN_ARMS.join(', ')})`)
}
if (arms.length === 0) invalid.push('--arms must name at least one arm')
if (new Set(arms).size !== arms.length) invalid.push(`--arms must be unique, got ${arms.join(',')}`)
const warmup = Boolean(args.warmup)
if (warmup && mode === 'cold') {
  invalid.push('--warmup cannot be combined with --mode cold: every cold measurement already gets a fresh worker')
}
/**
 * Cooling between blocks is DEFAULT-ON: every block after the first otherwise
 * starts on the previous one's heat, and the summary pools them. `--cooldown 0`
 * opts out and says so in the file's method warnings.
 */
const blockCooldownMs = intOption('cooldown', args.cooldown, {
  min: 0,
  max: 86_400_000,
  fallback: 60_000,
})

if (invalid.length > 0) {
  for (const problem of invalid) console.error(`! ${problem}`)
  process.exit(1)
}

/**
 * Digest of the built worker chunk.
 *
 * Vite names the chunk after the module and appends its content hash, so the
 * filename alone already distinguishes two builds — but the sha256 is what makes
 * a committed baseline checkable against a rebuild months later. `--skip-build`
 * is therefore harmless rather than a trap: whatever `dist` holds is what gets
 * recorded, built by this run or not.
 */
const workerBundleStamp = () => {
  try {
    const assets = resolve(repoRoot, 'dist', 'assets')
    const file = readdirSync(assets)
      .filter((name) => /^analysisWorker-.*\.js$/.test(name))
      .sort()
      .at(-1)
    if (!file) return { workerBundleFile: null, workerBundleSha256: null }
    return {
      workerBundleFile: `dist/assets/${file}`,
      workerBundleSha256: createHash('sha256')
        .update(readFileSync(resolve(assets, file)))
        .digest('hex'),
    }
  } catch {
    return { workerBundleFile: null, workerBundleSha256: null }
  }
}
const outPath = resolve(
  repoRoot,
  typeof args.out === 'string'
    ? args.out
    : `docs/analysis/device-baseline-desktop-${new Date().toISOString().slice(0, 10)}.jsonl`,
)

const run = (command, commandArgs, options = {}) =>
  new Promise((resolvePromise, reject) => {
    const child = spawn(command, commandArgs, {
      cwd: repoRoot,
      stdio: 'inherit',
      ...options,
    })
    child.on('error', reject)
    child.on('exit', (code) =>
      code === 0 ? resolvePromise() : reject(new Error(`${command} exited with ${code}`)),
    )
  })

const waitForServer = async (url, timeoutMs = 60_000) => {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url)
      if (response.ok) return
    } catch {
      // Not listening yet.
    }
    await new Promise((r) => setTimeout(r, 300))
  }
  throw new Error(`preview server did not start at ${url}`)
}

const main = async () => {
  if (!args['skip-build']) {
    console.log('› building the bench entry (BENCH=1 vite build)')
    await run('npx', ['vite', 'build'], { env: { ...process.env, BENCH: '1' } })
  }

  console.log(`› starting vite preview on :${port}`)
  // Bind explicitly: `vite preview` otherwise listens on `localhost`, which on
  // macOS resolves to ::1 first and leaves the 127.0.0.1 probe below failing.
  const previewHost = '127.0.0.1'
  const preview = spawn(
    'npx',
    ['vite', 'preview', '--host', previewHost, '--port', String(port), '--strictPort'],
    {
      cwd: repoRoot,
      stdio: 'ignore',
      env: process.env,
    },
  )

  const pageUrl = `http://${previewHost}:${port}/bench/device/index.html`
  let browser
  try {
    await waitForServer(pageUrl)
    browser = await chromium.launch()
    const page = await browser.newPage()
    page.on('console', (message) => {
      if (message.type() === 'error') console.error(`[page] ${message.text()}`)
    })
    await page.goto(pageUrl)
    // A `dist` built without BENCH=1 has no bench entry, so the preview server
    // answers with the SPA fallback and the run would measure nothing. Say so.
    if (await page.evaluate(() => typeof window.__ghostBench === 'undefined')) {
      throw new Error(
        'the bench page is not in dist — rebuild with `BENCH=1 vite build` (do not pass --skip-build)',
      )
    }

    console.log(`› running ${positionSetId} · repeats ${repeats} · mode ${mode}`)
    // No Playwright timeout on the run itself: a full thermal sequence at depth
    // 17 legitimately takes many minutes, and a harness timeout would discard a
    // valid measurement mid-flight.
    page.setDefaultTimeout(0)
    const records = await page.evaluate(
      async (config) => {
        const bench = window.__ghostBench
        if (!bench) throw new Error('__ghostBench is not exposed on this page')
        return bench.run(config)
      },
      {
        deviceLabel: label,
        notes: typeof args.notes === 'string' ? args.notes : 'desktop control, scripted run',
        mode,
        positionSetId,
        thermalPlies,
        repeats,
        arms,
        warmup,
        blockCooldownMs,
        // The page injects the git revision at build time; only a process that can
        // read `dist` can add the digest.
        source: workerBundleStamp(),
        ...(depth ? { depth } : {}),
      },
    )

    const jsonl = await page.evaluate(() => window.__ghostBench.jsonl())
    mkdirSync(dirname(outPath), { recursive: true })
    writeFileSync(outPath, jsonl)

    const moves = records.filter((record) => record.kind === 'move')
    const errors = moves.filter((record) => record.error).length
    console.log(`› wrote ${outPath}`)
    console.log(`› ${moves.length} measurements, ${errors} errored`)

    // Loudly, and last: a method warning that scrolls past unread is the whole
    // failure mode this exists to prevent.
    const summary = records.find((record) => record.kind === 'summary')
    for (const warning of summary?.methodWarnings ?? []) {
      console.warn(`! method: ${warning}`)
    }
    if (summary && summary.methodWarnings.length === 0) {
      console.log('› method: no warnings — quotable as a baseline')
    }
  } finally {
    await browser?.close()
    preview.kill()
  }
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
