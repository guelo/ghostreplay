import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  ABANDONED_AFTER_MS,
  databasePathFor,
  newRunToken,
  removeDatabase,
  slotIsIdle,
  sweepAbandonedDatabases,
} from './run-db.mjs'
import {
  DEFAULT_BACKEND_PORT_BASE,
  DEFAULT_FRONTEND_PORT_BASE,
  DEFAULT_RESERVATION_PORT_BASE,
} from './run-slot.mjs'

let dir

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'e2e-run-db-'))
})

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true })
})

/** Fresh by default; `ageMs` backdates it the way a run that stopped writing would. */
const write = (name, ageMs = 0) => {
  const file = path.join(dir, name)
  fs.writeFileSync(file, 'x')
  if (ageMs > 0) {
    const when = new Date(Date.now() - ageMs)
    fs.utimesSync(file, when, when)
  }
  return file
}

/** Old enough that an idle slot is proof the run behind it is over. */
const QUIET = ABANDONED_AFTER_MS + 60_000

const names = () => fs.readdirSync(dir).sort()

const busy = async () => false
const idle = async () => true

describe('databasePathFor', () => {
  /**
   * The property the whole design now rests on. The reservation socket cannot
   * span a run's process tree — a descriptor dies at the next exec and
   * Playwright's web servers sit in their own process groups — so a slot can be
   * released while something from that run is still seeding. Sharing a path is
   * what makes that silent, and two runs never share one.
   */
  it('gives two runs on the same slot different files', () => {
    expect(databasePathFor(dir, 3, newRunToken())).not.toBe(
      databasePathFor(dir, 3, newRunToken()),
    )
  })

  it('keeps the slot visible in the name', () => {
    expect(path.basename(databasePathFor(dir, 3, 'abcdef012345'))).toBe(
      'e2e-slot-3-abcdef012345.sqlite3',
    )
  })
})

describe('newRunToken', () => {
  /**
   * Not the pid. A pid is unique only among live processes, and the runs this
   * has to separate are exactly the ones with something outliving its runner: an
   * orphan that lasts until its old pid comes round again would hand a later run
   * on that slot the same filename.
   */
  it('does not repeat', () => {
    const tokens = new Set(Array.from({ length: 500 }, newRunToken))

    expect(tokens.size).toBe(500)
  })

  /** Change the token format and the sweep stops recognising its own files. */
  it('produces names the sweep will collect', async () => {
    const run = newRunToken()
    write(`e2e-slot-0-${run}.sqlite3`, QUIET)

    expect(await sweepAbandonedDatabases(dir, { slotIsFree: idle })).toEqual([
      `e2e-slot-0-${run}.sqlite3`,
    ])
  })
})

/**
 * The rollback journal is the one that actually appears — nothing in the backend
 * sets journal_mode, so `PRAGMA journal_mode` is `delete` and an open write
 * transaction means `<db>-journal`. WAL's pair is covered because turning it on
 * should not be able to break this quietly.
 */
const SIDECARS = ['-journal', '-wal', '-shm']

describe('removeDatabase', () => {
  it('takes every sqlite sidecar with it', () => {
    const db = write('e2e-slot-0-aaaaaaaaaaaa.sqlite3')
    for (const sidecar of SIDECARS) write(`e2e-slot-0-aaaaaaaaaaaa.sqlite3${sidecar}`)

    removeDatabase(db)

    expect(names()).toEqual([])
  })

  it('is silent when there is nothing to remove', () => {
    expect(() => removeDatabase(path.join(dir, 'absent.sqlite3'))).not.toThrow()
  })
})

describe('slotIsIdle', () => {
  it('probes the reservation and both service ports for that slot', async () => {
    const probed = []

    await slotIsIdle(2, async (port) => {
      probed.push(port)
      return true
    })

    expect(probed.sort()).toEqual(
      [
        DEFAULT_RESERVATION_PORT_BASE + 2,
        DEFAULT_FRONTEND_PORT_BASE + 2,
        DEFAULT_BACKEND_PORT_BASE + 2,
      ].sort(),
    )
  })

  /**
   * Each port catches a different survivor: the reservation covers a healthy run
   * and a runner killed while its Playwright carried on, the service ports cover
   * a Playwright that died and left its web servers behind.
   */
  it.each([
    ['reservation', DEFAULT_RESERVATION_PORT_BASE],
    ['frontend', DEFAULT_FRONTEND_PORT_BASE],
    ['backend', DEFAULT_BACKEND_PORT_BASE],
  ])('is busy while the %s port is held', async (_name, base) => {
    expect(await slotIsIdle(1, async (port) => port !== base + 1)).toBe(false)
  })

  it('is idle only when all three are free', async () => {
    expect(await slotIsIdle(1, idle)).toBe(true)
  })
})

describe('sweepAbandonedDatabases', () => {
  /**
   * The pid check this replaced got precisely one case wrong, and it was the
   * case the inherited reservation exists to create: SIGKILL the runner and
   * Playwright keeps running against a database whose named pid is now gone.
   * Asking the slot instead sees the reservation Playwright inherited.
   */
  it('keeps a database whose slot is still held', async () => {
    write('e2e-slot-0-aaaaaaaaaaaa.sqlite3', QUIET)

    expect(await sweepAbandonedDatabases(dir, { slotIsFree: busy })).toEqual([])
    expect(names()).toEqual(['e2e-slot-0-aaaaaaaaaaaa.sqlite3'])
  })

  /**
   * The reservation is what makes an idle slot mean "the run is over", and
   * reservationFd is null wherever the descriptor cannot be handed on. There a
   * runner killed during seeding leaves a live Playwright holding no port yet,
   * so the slot reads idle — including to the run that then takes it, which
   * skips the probe entirely. A database still being written is not litter on
   * any slot, held or probed.
   */
  it('keeps a database that is still being written, even on the slot this run holds', async () => {
    write('e2e-slot-0-aaaaaaaaaaaa.sqlite3')

    const removed = await sweepAbandonedDatabases(dir, { heldSlot: 0, slotIsFree: idle })

    expect(removed).toEqual([])
    expect(names()).toEqual(['e2e-slot-0-aaaaaaaaaaaa.sqlite3'])
  })

  /**
   * An open write transaction leaves the database file itself alone: the changes
   * sit in the sidecar until it commits. Reading only the database's mtime would
   * call a run that is mid-write quiet — a seeder in exactly the window where
   * the ports have nothing to say.
   */
  it.each(SIDECARS)('ages a database by its %s, not the database alone', async (sidecar) => {
    write('e2e-slot-0-aaaaaaaaaaaa.sqlite3', QUIET)
    write(`e2e-slot-0-aaaaaaaaaaaa.sqlite3${sidecar}`)

    expect(await sweepAbandonedDatabases(dir, { heldSlot: 0 })).toEqual([])
    expect(names()).toEqual([
      'e2e-slot-0-aaaaaaaaaaaa.sqlite3',
      `e2e-slot-0-aaaaaaaaaaaa.sqlite3${sidecar}`,
    ])
  })

  it('removes databases and sidecars once nothing holds the slot', async () => {
    write('e2e-slot-0-aaaaaaaaaaaa.sqlite3', QUIET)
    for (const sidecar of SIDECARS) write(`e2e-slot-0-aaaaaaaaaaaa.sqlite3${sidecar}`, QUIET)

    const removed = await sweepAbandonedDatabases(dir, { slotIsFree: idle })

    expect(removed).toEqual(['e2e-slot-0-aaaaaaaaaaaa.sqlite3'])
    expect(names()).toEqual([])
  })

  /** A sidecar whose database is already gone is still litter, and still ours. */
  it.each(SIDECARS)('collects an orphaned %s', async (sidecar) => {
    write(`e2e-slot-0-bbbbbbbbbbbb.sqlite3${sidecar}`, QUIET)

    const removed = await sweepAbandonedDatabases(dir, { slotIsFree: idle })

    expect(removed).toEqual(['e2e-slot-0-bbbbbbbbbbbb.sqlite3'])
    expect(names()).toEqual([])
  })

  it('decides per slot, so one busy slot does not protect another', async () => {
    write('e2e-slot-0-aaaaaaaaaaaa.sqlite3', QUIET)
    write('e2e-slot-7-bbbbbbbbbbbb.sqlite3', QUIET)

    const removed = await sweepAbandonedDatabases(dir, { slotIsFree: async (slot) => slot === 7 })

    expect(removed).toEqual(['e2e-slot-7-bbbbbbbbbbbb.sqlite3'])
    expect(names()).toEqual(['e2e-slot-0-aaaaaaaaaaaa.sqlite3'])
  })

  /**
   * acquireRunSlot found all three of this slot's ports free moments ago, and
   * the file has been quiet since before that. Probing would say "busy" — we are
   * the ones holding it — and litter on the slot everyone lands on first would
   * then never be collected.
   */
  it('sweeps the caller’s own slot without probing it', async () => {
    write('e2e-slot-4-aaaaaaaaaaaa.sqlite3', QUIET)
    const probed = []

    const removed = await sweepAbandonedDatabases(dir, {
      heldSlot: 4,
      slotIsFree: async (slot) => {
        probed.push(slot)
        return false
      },
    })

    expect(removed).toEqual(['e2e-slot-4-aaaaaaaaaaaa.sqlite3'])
    expect(probed).toEqual([])
  })

  it('binds no ports when there is nothing old enough to collect', async () => {
    write('e2e-slot-9-aaaaaaaaaaaa.sqlite3')
    const probed = []

    await sweepAbandonedDatabases(dir, {
      slotIsFree: async (slot) => {
        probed.push(slot)
        return true
      },
    })

    expect(probed).toEqual([])
  })

  it('leaves everything else in the directory alone', async () => {
    // backend/.tmp is shared with other tooling, and with pytest runs that are
    // still colliding on their own fixed names (g-pytest-tmp-collide).
    write('e2e.sqlite3', QUIET)
    write('pytest-scratch.sqlite3', QUIET)
    write('e2e-slot-0-48213.sqlite3', QUIET) // slot-shaped but not a run token

    expect(await sweepAbandonedDatabases(dir, { slotIsFree: idle })).toEqual([])
    expect(names()).toEqual([
      'e2e-slot-0-48213.sqlite3',
      'e2e.sqlite3',
      'pytest-scratch.sqlite3',
    ])
  })

  it('reports nothing for a directory that does not exist yet', async () => {
    expect(await sweepAbandonedDatabases(path.join(dir, 'absent'))).toEqual([])
  })
})
