import { spawn } from 'node:child_process'
import net from 'node:net'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'

import {
  DEFAULT_BACKEND_PORT_BASE,
  DEFAULT_FRONTEND_PORT_BASE,
  DEFAULT_RESERVATION_PORT_BASE,
  acquireRunSlot,
  isPortFree,
  reservePort,
} from './run-slot.mjs'

const moduleUrl = new URL('./run-slot.mjs', import.meta.url).href

/**
 * These tests need a port range of their own, twice over.
 *
 * Not the real bases: those belong to actual e2e runs, and a test that reserved
 * them would steal slots from a suite someone is running in the next terminal.
 * And not a fixed range either, because this file is in the pre-push gate — two
 * agents pushing at once run it concurrently, and the assertions below name
 * specific slot indices, so a shared range would fail them exactly the way the
 * module under test exists to prevent. That is not a hypothetical: a fixed 8700
 * base failed both of two concurrent gate runs.
 *
 * So each run of this file leases one lease port and derives a private range of
 * SLOT_COUNT reservation ports from it. The lease uses reservePort directly
 * rather than acquireRunSlot: isolating the tests of the allocator with the
 * allocator would let a bug in it hide a bug in it.
 */
const RANGE_LEASE_BASE = 8700
const RANGE_LEASE_COUNT = 8
const RANGE_BASE = 8710
const RANGE_STRIDE = 32 // > SLOT_COUNT, so leased ranges cannot overlap
const SLOT_COUNT = 24

let rangeLease = null
let RESERVATION_PORT_BASE = RANGE_BASE

beforeAll(async () => {
  for (let index = 0; index < RANGE_LEASE_COUNT; index += 1) {
    const lease = await reservePort(RANGE_LEASE_BASE + index)
    if (lease === null) continue
    rangeLease = lease
    RESERVATION_PORT_BASE = RANGE_BASE + index * RANGE_STRIDE
    return
  }
  throw new Error(
    `No free test range: ${RANGE_LEASE_COUNT} concurrent runs of this file already hold ` +
      `${RANGE_LEASE_BASE}-${RANGE_LEASE_BASE + RANGE_LEASE_COUNT - 1}.`,
  )
})

afterAll(async () => {
  if (rangeLease !== null) await new Promise((resolve) => rangeLease.close(resolve))
})

/** The kernel unbinds a moment after the holder dies; give it that moment. */
const waitForFree = async (port) => {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (await isPortFree(port)) return true
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
  return false
}

/** Never probe real ports except in the tests that are specifically about them. */
const alwaysFree = async () => true

const acquire = (overrides = {}) =>
  acquireRunSlot({
    reservationPortBase: RESERVATION_PORT_BASE,
    slotCount: SLOT_COUNT,
    portIsFree: alwaysFree,
    ...overrides,
  })

const held = []
const track = async (slot) => {
  held.push(slot)
  return slot
}

afterEach(async () => {
  while (held.length > 0) await held.pop().release()
})

describe('acquireRunSlot', () => {
  it('derives every port from the slot index', async () => {
    const slot = await track(await acquire())

    expect(slot.slot).toBe(0)
    expect(slot.frontendPort).toBe(DEFAULT_FRONTEND_PORT_BASE)
    expect(slot.backendPort).toBe(DEFAULT_BACKEND_PORT_BASE)
    expect(slot.reservationPort).toBe(RESERVATION_PORT_BASE)
  })

  it('hands overlapping runs different slots', async () => {
    const first = await track(await acquire())
    const second = await track(await acquire())
    const third = await track(await acquire())

    expect(new Set([first.slot, second.slot, third.slot]).size).toBe(3)
    expect(new Set([first.backendPort, second.backendPort, third.backendPort]).size).toBe(3)
  })

  it('reuses a slot once its holder releases', async () => {
    const first = await acquire()
    await first.release()

    const second = await track(await acquire())

    expect(second.slot).toBe(first.slot)
  })

  it('release is idempotent', async () => {
    const first = await acquire()
    await first.release()
    const second = await track(await acquire())

    await first.release() // late cleanup, after the slot moved on

    expect(await isPortFree(second.reservationPort)).toBe(false)
  })

  it('skips a slot whose ports are already bound', async () => {
    const slot = await track(
      await acquire({ portIsFree: async (port) => port !== DEFAULT_BACKEND_PORT_BASE }),
    )

    expect(slot.slot).toBe(1)
    // The skipped slot must not stay reserved, or a transient port holder would
    // retire it for every later run on this machine.
    expect(await isPortFree(RESERVATION_PORT_BASE)).toBe(true)
  })

  it('fails with instructions when every slot is taken', async () => {
    for (let i = 0; i < 2; i += 1) await track(await acquire({ slotCount: 2 }))

    await expect(acquire({ slotCount: 2 })).rejects.toThrow(
      /No free e2e run slot[\s\S]*E2E_FRONTEND_PORT/,
    )
  })
})

describe('isPortFree', () => {
  it('reports a bound port as busy and an unbound one as free', async () => {
    const server = net.createServer()
    const port = await new Promise((resolve) => {
      server.listen({ port: 0, host: '127.0.0.1' }, () => resolve(server.address().port))
    })

    try {
      expect(await isPortFree(port)).toBe(false)
    } finally {
      await new Promise((resolve) => server.close(resolve))
    }

    expect(await isPortFree(port)).toBe(true)
  })
})

/**
 * The collision this module exists to prevent happens BETWEEN `npm run test:e2e`
 * processes. Nothing in-process can reproduce it — a single-threaded allocator
 * never interleaves with itself — so these spawn real ones. The first draft of
 * this module passed every test above and still handed one slot to two of the
 * six children here.
 */
describe('concurrent processes', () => {
  const spawnRun = (holdMs) =>
    new Promise((resolve, reject) => {
      const source = `
        const { acquireRunSlot } = await import(${JSON.stringify(moduleUrl)})
        const slot = await acquireRunSlot({
          reservationPortBase: ${RESERVATION_PORT_BASE},
          slotCount: ${SLOT_COUNT},
          portIsFree: async () => true,
        })
        process.stdout.write(String(slot.slot))
        await new Promise((resolve) => setTimeout(resolve, ${holdMs}))
      `
      const child = spawn(process.execPath, ['--input-type=module', '-e', source], {
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      let stdout = ''
      let stderr = ''
      child.stdout.on('data', (chunk) => (stdout += chunk))
      child.stderr.on('data', (chunk) => (stderr += chunk))
      child.on('error', reject)
      child.on('exit', (code) => {
        if (code === 0) resolve(stdout.trim())
        else reject(new Error(stderr || `exit ${code}`))
      })
    })

  it('never hands the same slot to two processes at once', async () => {
    const RUNS = 6

    const slots = await Promise.all(Array.from({ length: RUNS }, () => spawnRun(500)))

    expect(slots).toHaveLength(RUNS)
    expect(new Set(slots).size).toBe(RUNS)
  }, 20_000)

  it('frees a slot when its holder is killed', async () => {
    // No pid liveness check to get wrong: the kernel unbinds on death, so a
    // SIGKILLed run cannot retire a slot the way a stale lockfile would.
    const source = `
      const { acquireRunSlot } = await import(${JSON.stringify(moduleUrl)})
      const slot = await acquireRunSlot({
        reservationPortBase: ${RESERVATION_PORT_BASE},
        slotCount: ${SLOT_COUNT},
        portIsFree: async () => true,
      })
      process.stdout.write(String(slot.slot))
      setInterval(() => {}, 1000)
    `
    const child = spawn(process.execPath, ['--input-type=module', '-e', source], {
      stdio: ['ignore', 'pipe', 'inherit'],
    })
    const claimed = await new Promise((resolve) => {
      child.stdout.once('data', (chunk) => resolve(Number(String(chunk).trim())))
    })
    expect(await isPortFree(RESERVATION_PORT_BASE + claimed)).toBe(false)

    child.kill('SIGKILL')
    await new Promise((resolve) => child.on('exit', resolve))

    expect(await isPortFree(RESERVATION_PORT_BASE + claimed)).toBe(true)
  }, 20_000)

  it('holds the slot for a child that outlives the process which reserved it', async () => {
    // One edge of the real topology: runner → `playwright test`. SIGKILL the
    // runner and the suite keeps going, so its ports must stay reserved rather
    // than being handed to the next run mid-flight — and the database sweep
    // reads that same reservation as "something is still on this slot".
    //
    // Only that edge. The descriptor dies at the next exec, so it does not carry
    // on into Playwright's web servers, and no test here should be read as
    // saying a database is safe because a port is held; that comes from naming
    // the file for the run (run-db.mjs).
    const source = `
      const { spawn } = await import('node:child_process')
      const { acquireRunSlot } = await import(${JSON.stringify(moduleUrl)})
      const slot = await acquireRunSlot({
        reservationPortBase: ${RESERVATION_PORT_BASE},
        slotCount: ${SLOT_COUNT},
        portIsFree: async () => true,
      })
      const work = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 30000)'], {
        stdio: ['ignore', 'ignore', 'ignore', slot.reservationFd],
        detached: true,
      })
      work.unref()
      process.stdout.write(JSON.stringify({ slot: slot.slot, pid: work.pid }))
      setInterval(() => {}, 1000)
    `
    const runner = spawn(process.execPath, ['--input-type=module', '-e', source], {
      stdio: ['ignore', 'pipe', 'inherit'],
    })
    const { slot, pid } = await new Promise((resolve) => {
      runner.stdout.once('data', (chunk) => resolve(JSON.parse(String(chunk))))
    })
    const reservationPort = RESERVATION_PORT_BASE + slot

    try {
      runner.kill('SIGKILL')
      await new Promise((resolve) => runner.on('exit', resolve))

      expect(await isPortFree(reservationPort)).toBe(false)
    } finally {
      process.kill(pid, 'SIGKILL')
    }

    // ...and released once the last of them is gone, so nothing retires a slot.
    expect(await waitForFree(reservationPort)).toBe(true)
  }, 20_000)
})
