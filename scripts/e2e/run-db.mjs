import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import {
  DEFAULT_BACKEND_PORT_BASE,
  DEFAULT_FRONTEND_PORT_BASE,
  DEFAULT_RESERVATION_PORT_BASE,
  isPortFree,
} from './run-slot.mjs'

/**
 * Naming and cleanup for the per-run e2e database (g-e2e-port-collide).
 *
 * The database is named for the RUN, not for the slot, and that distinction is
 * the whole point. A slot reservation is a listening socket, so it lives exactly
 * as long as the processes holding that socket — and a run is a tree: runner →
 * `playwright test` → the two web servers. The runner can hand its socket to
 * Playwright, but no further: the descriptor is closed on the next exec, and
 * Playwright puts each web server in its own process group, so there is no group
 * to kill either. Kill Playwright and its servers are orphaned with nothing left
 * holding the slot.
 *
 * That gap is worst during seeding. `start_backend.sh` runs
 * `seed_e2e_data.py --reset` BEFORE uvicorn binds, so an orphaned seeder holds
 * no port at all, the next run's port probe sees a free slot, and two --reset
 * processes end up on one file — the UNIQUE-constraint corruption this bead was
 * filed for. Chasing that with process bookkeeping means asking "is anything
 * from that run still alive?", which is exactly the stale-pid question a
 * lockfile would have posed, and it is no easier here.
 *
 * So a random token goes in the filename and the question never arises. Two runs
 * cannot name the same file, whatever survives whatever. Ports can still be
 * contested in that seeding window, but losing a port is loud and immediate —
 * uvicorn or Vite fails to bind and Playwright says so. Silence was the problem.
 *
 * The question does come back for cleanup, which has to decide whether a file is
 * litter. It is answerable there because the stakes are reversed: a sweep that
 * guesses wrong leaves a few kB in a gitignored directory, so it can afford to
 * be wrong only in that direction. See sweepAbandonedDatabases.
 */

/**
 * Everything sqlite can leave beside the database, and the list every part of
 * this module works from — matching, ageing and deletion have to agree or a
 * sidecar becomes invisible to one of them.
 *
 * `-journal` is the one that actually turns up: nothing in the backend sets
 * journal_mode, so sqlite uses the default rollback journal, and a write
 * transaction creates `<db>-journal` for as long as it is open. `-wal`/`-shm`
 * are here so that turning WAL on later cannot quietly break this.
 */
const DATABASE_SIDECARS = ['-journal', '-wal', '-shm']
const DATABASE_SUFFIXES = ['', ...DATABASE_SIDECARS]

/** Journals too: a stale journal against a fresh database is worse than neither. */
const unlinkWithJournals = (databasePath) => {
  for (const suffix of DATABASE_SUFFIXES) {
    try {
      fs.unlinkSync(`${databasePath}${suffix}`)
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
    }
  }
}

const RUN_TOKEN_BYTES = 6

/**
 * Identity for one run of the suite.
 *
 * Deliberately not the pid, which is unique only among live processes. The runs
 * this has to keep apart are exactly the ones where something outlived its
 * runner, and an orphan that survives long enough for its old pid to be handed
 * out again would give a later run on that slot the same filename — the sharing
 * this module exists to make impossible, arriving by the back door. 48 random
 * bits are unique for as long as the file exists, which is the property the name
 * actually needs.
 */
export const newRunToken = () => crypto.randomBytes(RUN_TOKEN_BYTES).toString('hex')

export const databasePathFor = (directory, slot, run) =>
  path.join(directory, `e2e-slot-${slot}-${run}.sqlite3`)

export const removeDatabase = unlinkWithJournals

/** Matches what databasePathFor writes, plus the sidecars sqlite puts beside it. */
const ABANDONED = new RegExp(
  `^(e2e-slot-(\\d+)-[0-9a-f]{${RUN_TOKEN_BYTES * 2}}\\.sqlite3)(${DATABASE_SIDECARS.join('|')})?$`,
)

const SLOT_PORT_BASES = [
  DEFAULT_RESERVATION_PORT_BASE,
  DEFAULT_FRONTEND_PORT_BASE,
  DEFAULT_BACKEND_PORT_BASE,
]

/**
 * Whether anything at all from a run is still on this slot.
 *
 * All three ports, because each covers a different survivor. The reservation is
 * held by the runner and by the Playwright it spawned, so it covers both a
 * healthy run and a runner that was killed while its suite kept going. The
 * frontend and backend ports cover the opposite case, where Playwright died and
 * left its web servers behind holding nothing else.
 */
export const slotIsIdle = async (slot, portIsFree = isPortFree) => {
  for (const base of SLOT_PORT_BASES) {
    if (!(await portIsFree(base + slot))) return false
  }
  return true
}

/**
 * How long a file must lie untouched before an idle slot is taken as proof its
 * run is over.
 *
 * The window this has to cover is small and specific: from the moment the seeder
 * creates the database to the moment the servers bind. Before that bind there is
 * nothing on the slot's ports to find, so a run in the middle of seeding looks
 * exactly like a run that is over. Seeding takes seconds; ten minutes is two
 * orders of magnitude of headroom, and being wrong the other way costs one file
 * surviving until the next run ten minutes later.
 */
export const ABANDONED_AFTER_MS = 10 * 60 * 1000

/**
 * Newest of the database and its sidecars. A write transaction can leave the
 * database file itself untouched for as long as it stays open — the changes sit
 * in `-journal` (or `-wal`) until it commits — so reading the database's mtime
 * alone would call a busy run quiet.
 */
const lastWrittenMs = (databasePath) => {
  let newest = 0
  for (const suffix of DATABASE_SUFFIXES) {
    try {
      newest = Math.max(newest, fs.statSync(`${databasePath}${suffix}`).mtimeMs)
    } catch (error) {
      if (error.code !== 'ENOENT') throw error
    }
  }
  return newest
}

/**
 * Delete databases left behind by runs that are over.
 *
 * The naive test — is the pid in the name still running? — is wrong in the one
 * case the reservation was built to survive: SIGKILL the runner and Playwright
 * carries on with the slot still held, so the pid is gone while the database is
 * very much in use. Asking the slot instead sees the reservation Playwright
 * inherited, and sees the web servers when it is Playwright that died.
 *
 * Both of those readings depend on the descriptor hand-off, and reservationFd
 * returns null where that is unavailable. Then a runner killed during seeding
 * leaves a live Playwright behind no ports at all, and its database is
 * indistinguishable by slot from litter — which is why idleness alone is not
 * allowed to condemn a file. A run writes to its database from the moment the
 * seeder creates it; one that has been untouched for ABANDONED_AFTER_MS on an
 * idle slot is over. The two together hold whether or not the hand-off worked,
 * which is the point: nothing about database safety may rest on the reservation.
 *
 * `heldSlot` is this run's own slot, swept without a probe — acquireRunSlot
 * found all three of its ports free a moment ago. It is the age check that makes
 * that safe rather than the probe it skips, since a run seeding on that slot in
 * the no-descriptor fallback would have passed the probe too. Without it, litter
 * on the busiest slot would only ever be collected while nobody was using it.
 *
 * @returns {Promise<string[]>} the files removed, for logging
 */
export const sweepAbandonedDatabases = async (directory, options = {}) => {
  const { heldSlot = null, slotIsFree = slotIsIdle } = options

  let entries
  try {
    entries = fs.readdirSync(directory)
  } catch (error) {
    if (error.code === 'ENOENT') return []
    throw error
  }

  const bySlot = new Map()
  for (const entry of entries.sort()) {
    const match = ABANDONED.exec(entry)
    if (match === null) continue
    // Journals collapse onto the database they belong to; unlinkWithJournals
    // takes them, and a journal whose database is already gone still needs one.
    const database = path.join(directory, match[1])
    if (Date.now() - lastWrittenMs(database) < ABANDONED_AFTER_MS) continue
    const slot = Number(match[2])
    if (!bySlot.has(slot)) bySlot.set(slot, new Set())
    bySlot.get(slot).add(database)
  }

  const removed = []
  for (const [slot, databases] of bySlot) {
    // Probed only once there is something to collect, so the ordinary run —
    // nothing left behind — touches no ports at all.
    if (slot !== heldSlot && !(await slotIsFree(slot))) continue
    for (const database of databases) {
      unlinkWithJournals(database)
      removed.push(path.basename(database))
    }
  }
  return removed
}
