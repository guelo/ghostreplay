import { useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import {
  clear,
  getEntries,
  subscribe,
  type LogEntry,
  type LogLevel,
} from '../utils/debugLog'
import './DebugOverlay.css'

const LEVELS: LogLevel[] = ['log', 'info', 'warn', 'error', 'debug', 'net']

function formatTs(ts: number): string {
  const d = new Date(ts)
  return d.toLocaleTimeString(undefined, { hour12: false }) +
    '.' + String(d.getMilliseconds()).padStart(3, '0')
}

export default function DebugOverlay() {
  // ?debug=1 auto-opens on mount.
  const [open, setOpen] = useState(
    () => new URLSearchParams(window.location.search).get('debug') === '1',
  )
  const [levelFilter, setLevelFilter] = useState<Set<LogLevel>>(new Set(LEVELS))
  const [textFilter, setTextFilter] = useState('')

  const entries = useSyncExternalStore(subscribe, getEntries)

  // Ctrl+Shift+D desktop chord.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) {
        e.preventDefault()
        setOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const filtered = useMemo(() => {
    const q = textFilter.toLowerCase()
    return entries.filter(
      (e: LogEntry) =>
        levelFilter.has(e.level) && (q === '' || e.args.toLowerCase().includes(q)),
    )
  }, [entries, levelFilter, textFilter])

  const toggleLevel = (level: LogLevel) => {
    setLevelFilter((prev) => {
      const next = new Set(prev)
      if (next.has(level)) next.delete(level)
      else next.add(level)
      return next
    })
  }

  const copyAll = () => {
    const text = filtered
      .map((e) => `${formatTs(e.ts)} [${e.level}] ${e.args}`)
      .join('\n')
    void navigator.clipboard?.writeText(text)
  }

  return (
    <>
      {/* Discreet mobile corner hotspot. */}
      <button
        type="button"
        className="debug-hotspot"
        aria-label="Open debug overlay"
        onClick={() => setOpen((o) => !o)}
      />

      {open && (
        <div className="debug-overlay" role="dialog" aria-label="Debug logs">
          <div className="debug-overlay__header">
            <span className="debug-overlay__title">Debug logs</span>
            <span className="debug-overlay__count">
              {filtered.length}/{entries.length}
            </span>
            <button type="button" onClick={copyAll}>Copy</button>
            <button type="button" onClick={() => clear()}>Clear</button>
            <button type="button" onClick={() => setOpen(false)}>✕</button>
          </div>

          <div className="debug-overlay__filters">
            {LEVELS.map((level) => (
              <button
                key={level}
                type="button"
                className={
                  'debug-chip debug-chip--' + level +
                  (levelFilter.has(level) ? ' is-active' : '')
                }
                onClick={() => toggleLevel(level)}
              >
                {level}
              </button>
            ))}
            <input
              type="text"
              className="debug-overlay__search"
              placeholder="filter…"
              value={textFilter}
              onChange={(e) => setTextFilter(e.target.value)}
            />
          </div>

          <ol className="debug-overlay__list">
            {filtered
              .slice()
              .reverse()
              .map((e) => (
                <li
                  key={e.id}
                  className={
                    'debug-entry debug-entry--' + e.level +
                    (e.level === 'net' && e.net && !e.net.ok ? ' debug-entry--net-fail' : '')
                  }
                >
                  <span className="debug-entry__ts">{formatTs(e.ts)}</span>
                  <span className="debug-entry__level">{e.level}</span>
                  <pre className="debug-entry__args">{e.args}</pre>
                </li>
              ))}
          </ol>
        </div>
      )}
    </>
  )
}
