/**
 * Device benchmark page shell (g-two-search-grade §10.1 bullet 2).
 *
 * Deliberately plain DOM and no framework: this page's own rendering must not
 * appear in the latency it measures. Every DOM write happens BETWEEN
 * measurements, never while a search is running, and the JSONL textarea is
 * rewritten only on completion.
 *
 * All logic lives in the tested modules (`runner`, `transcript`, `summarize`,
 * `schedule`); this file is the shell that wires them to controls and exposes
 * `window.__ghostBench` so the Playwright baseline driver can run the same
 * entry point headlessly.
 */

import type { BenchMoveRecord, BenchRecord, BenchSummaryRecord } from '../benchRecord'
import { serializeJsonl } from '../benchRecord'
import { latencySeriesByMoveIndex } from '../summarize'
import { renderLatencyChart } from './chart'
import type { BenchRunConfig } from './config'
import { configProblems } from './config'
import { VALUE_CONTROL_IDS, benchFormControls, readConfig, selectedArms } from './form'
import type { BenchRunHandle } from './runner'
import { runBench } from './runner'
import { createAnalysisWorker } from './workerFactory'

const CONFIG_STORAGE_KEY = 'ghost-bench-device-config'

const byId = <T extends HTMLElement>(id: string) => document.getElementById(id) as T

const startButton = byId<HTMLButtonElement>('start')
const stopButton = byId<HTMLButtonElement>('stop')
const copyButton = byId<HTMLButtonElement>('copy')
const downloadButton = byId<HTMLButtonElement>('download')
const statusLine = byId<HTMLParagraphElement>('status')
const warningList = byId<HTMLUListElement>('warnings')
const summaryTable = byId<HTMLTableElement>('summary')
const chartHost = byId<HTMLDivElement>('chart')
const jsonlBox = byId<HTMLTextAreaElement>('jsonl')

const controls = benchFormControls(document)

const persistConfig = () => {
  try {
    localStorage.setItem(
      CONFIG_STORAGE_KEY,
      JSON.stringify({
        ...Object.fromEntries(VALUE_CONTROL_IDS.map((id) => [id, controls[id].value])),
        arms: selectedArms(controls),
        warmup: controls.warmup.checked,
      }),
    )
  } catch {
    // A private-mode phone may refuse storage; the run does not depend on it.
  }
}

const restoreConfig = () => {
  try {
    const stored = localStorage.getItem(CONFIG_STORAGE_KEY)
    if (!stored) return
    const values = JSON.parse(stored) as Record<string, unknown>
    for (const id of VALUE_CONTROL_IDS) {
      if (typeof values[id] === 'string') controls[id].value = values[id] as string
    }
    if (Array.isArray(values.arms)) {
      for (const box of controls.armBoxes) box.checked = values.arms.includes(box.value)
    }
    if (typeof values.warmup === 'boolean') controls.warmup.checked = values.warmup
  } catch {
    // Ignore a corrupt or unreadable stored config.
  }
}

/**
 * Method departures, shown beside the numbers they qualify.
 *
 * A run with one repeat produces a perfectly ordinary-looking summary, so the
 * page has to say out loud that it is not evidence — the JSONL carries the same
 * list in its run header and summary.
 */
const renderWarnings = (warnings: readonly string[]) => {
  warningList.innerHTML = warnings
    .map((warning) => `<li>${warning.replace(/&/g, '&amp;').replace(/</g, '&lt;')}</li>`)
    .join('')
}

const setStatus = (text: string, isError = false) => {
  statusLine.textContent = text
  statusLine.classList.toggle('error', isError)
}

const formatMs = (value: number) => (Number.isFinite(value) ? `${Math.round(value)}` : '—')

const renderSummary = (summary: BenchSummaryRecord) => {
  const rows = summary.cells
    .map(
      (cell) => `
        <tr>
          <td>${cell.arm} · ${cell.cohort} · ${cell.split}</td>
          <td>${cell.stats.n}</td>
          <td>${formatMs(cell.stats.medianMs)}</td>
          <td>${formatMs(cell.stats.p90Ms)}</td>
          <td>${formatMs(cell.stats.p95Ms)}</td>
          <td>${formatMs(cell.stats.worstMs)}</td>
          <td>${cell.stats.medianNodes === null ? '—' : Math.round(cell.stats.medianNodes)}</td>
        </tr>`,
    )
    .join('')

  const weighted = summary.gameWeighted
    .map((entry) =>
      entry.medianMs === null
        ? `${entry.arm}: —`
        : `${entry.arm}: ${formatMs(entry.medianMs)} med / ${formatMs(entry.p95Ms ?? NaN)} p95`,
    )
    .join(' · ')
  const match = summary.observedMatchRate
    .map((entry) => `${entry.arm}: ${entry.m === null ? '—' : `${Math.round(entry.m * 100)}%`} (n=${entry.n})`)
    .join(' · ')

  summaryTable.innerHTML = `
    <thead>
      <tr><th>arm · cohort · split</th><th>n</th><th>median ms</th><th>p90</th><th>p95</th><th>worst</th><th>median nodes</th></tr>
    </thead>
    <tbody>${rows}</tbody>
    <tfoot>
      <tr><td colspan="7" class="muted">${summary.completion} — ${summary.measuredItems}/${summary.plannedItems} measured${summary.warmupItems > 0 ? ` (+${summary.warmupItems} warm-up)` : ''} · game-weighted — ${weighted} · observed P===B — ${match} · errors ${summary.errors} · §4 divergence ${summary.legacySelectorDivergence}</td></tr>
    </tfoot>`
  renderWarnings(summary.methodWarnings)
}

const isMove = (record: BenchRecord): record is BenchMoveRecord => record.kind === 'move'

let handle: BenchRunHandle | null = null

const start = async (overrides: Partial<BenchRunConfig> = {}) => {
  if (handle) return []

  const config = { ...readConfig(controls), ...overrides }
  // The guard the runner and the CLI driver apply, run here too so it lands
  // before anything is disabled or a worker boots — and so the operator reads
  // every problem at once instead of one per attempt. `runBench` still refuses
  // the same configuration; this only makes the refusal legible and early.
  const problems = configProblems(config)
  if (problems.length > 0) {
    setStatus(problems.join(' · '), true)
    throw new Error(`invalid bench configuration: ${problems.join('; ')}`)
  }
  persistConfig()
  renderWarnings([])

  const records: BenchRecord[] = []
  startButton.disabled = true
  stopButton.disabled = false
  copyButton.disabled = true
  downloadButton.disabled = true
  setStatus('Booting the worker…')

  handle = runBench(config, {
    createWorker: createAnalysisWorker,
    onRecord: (record) => {
      records.push(record)
      if (record.kind === 'run' && record.methodWarnings.length > 0) {
        // Before the first measurement, so an operator can stop and fix the
        // configuration instead of discovering it at the end of a 40-move run.
        renderWarnings(record.methodWarnings)
      }
      if (record.kind === 'summary') {
        renderSummary(record)
      }
    },
    onProgress: (progress) => {
      // Between measurements only: a DOM write during a search would land in the
      // very latency this page exists to measure.
      if (progress.phase === 'booting') {
        setStatus(`Booting worker for block ${progress.blockIndex + 1}/${progress.blockCount}…`)
        return
      }
      if (progress.phase === 'cooling') {
        setStatus(
          `Cooling ${Math.round((progress.cooldownMs ?? 0) / 1000)}s before block ${progress.blockIndex + 1}/${progress.blockCount}…`,
        )
        return
      }
      const last = progress.lastRecord
      const tail = last
        ? ` · last ${Math.round(last.e2eMs)} ms${last.error ? ` · ERROR ${last.error}` : ''}`
        : ''
      setStatus(
        `${progress.phase} · ${progress.done}/${progress.total} · block ${progress.blockIndex + 1}/${progress.blockCount} · ${progress.positionId}${tail}`,
        Boolean(last?.error),
      )
    },
  })

  try {
    const finished = await handle.promise
    jsonlBox.value = serializeJsonl(finished)
    chartHost.innerHTML = renderLatencyChart(
      latencySeriesByMoveIndex(finished.filter(isMove)).map((series) => ({
        label: series.arm,
        points: series.points,
      })),
    )
    copyButton.disabled = false
    downloadButton.disabled = false
    setStatus(`Done — ${finished.filter(isMove).length} measurements. Copy or download the JSONL.`)
    return finished
  } catch (error) {
    jsonlBox.value = serializeJsonl(records)
    copyButton.disabled = records.length === 0
    downloadButton.disabled = records.length === 0
    setStatus(error instanceof Error ? error.message : String(error), true)
    throw error
  } finally {
    handle = null
    startButton.disabled = false
    stopButton.disabled = true
  }
}

startButton.addEventListener('click', () => {
  void start().catch(() => {
    // Already surfaced in the status line.
  })
})

stopButton.addEventListener('click', () => {
  handle?.stop()
  setStatus('Stopping after the current measurement…')
})

copyButton.addEventListener('click', () => {
  void navigator.clipboard?.writeText(jsonlBox.value).then(
    () => setStatus('JSONL copied to the clipboard.'),
    () => {
      jsonlBox.select()
      setStatus('Clipboard blocked — the JSONL is selected, copy it manually.', true)
    },
  )
})

downloadButton.addEventListener('click', () => {
  const blob = new Blob([jsonlBox.value], { type: 'application/x-ndjson' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  const label = controls.deviceLabel.value.trim().replace(/[^a-z0-9]+/gi, '-').toLowerCase()
  link.href = url
  link.download = `device-bench-${label || 'run'}-${new Date().toISOString().slice(0, 10)}.jsonl`
  link.click()
  URL.revokeObjectURL(url)
})

restoreConfig()

declare global {
  interface Window {
    __ghostBench?: {
      run: (overrides?: Partial<BenchRunConfig>) => Promise<BenchRecord[]>
      stop: () => void
      jsonl: () => string
    }
  }
}

// The automation entry point the Playwright baseline driver calls. Same code
// path as the button, so a scripted desktop-control run and an operator's phone
// run are the same measurement.
window.__ghostBench = {
  run: (overrides) => start(overrides),
  stop: () => handle?.stop(),
  jsonl: () => jsonlBox.value,
}
