import net from 'node:net'

/**
 * Per-run port/database isolation for the e2e stack (g-e2e-port-collide).
 *
 * The suite used to bind a fixed 4173/8010 and a fixed sqlite file, so two runs
 * on one machine could not coexist. Losing that race was survivable when it
 * failed fast; what made it worth fixing is that it often did not. Two seed
 * scripts hitting the same sqlite file raise UNIQUE constraint errors, and a run
 * that wins the port race but shares the database with a concurrent --reset sees
 * rows vanish mid-test. That reads exactly like a product regression, and it has
 * failed the pre-push gate for changes that could not possibly have caused it.
 *
 * A slot is a (frontend port, backend port) pair, reserved for as long as any
 * process from the run holds it. The database is not part of the slot: it is
 * named for the run, because a reservation cannot outlive the tree that holds it
 * and database safety must not depend on which process dies first (run-db.mjs).
 */

export const DEFAULT_SLOT_COUNT = 24
export const DEFAULT_FRONTEND_PORT_BASE = 4300
export const DEFAULT_BACKEND_PORT_BASE = 8300
export const DEFAULT_RESERVATION_PORT_BASE = 8400

/**
 * The reservation is a listening socket, not a lockfile.
 *
 * A lockfile needs a machine-wide directory (the colliding runs are usually in
 * different git worktrees, and AGENTS.md points TMPDIR inside the repo, so both
 * the obvious paths are per-worktree), and it needs the run's pid so a killed
 * run does not retire a slot forever. That pid is where lockfiles go wrong:
 * O_EXCL creates the file EMPTY and the pid lands in a second write, so a
 * concurrent run can read the gap, judge the lock corrupt, and take a slot
 * another process already holds. An earlier draft of this module did exactly
 * that, and run-slot.test.mjs caught it handing one slot to two processes.
 *
 * Binding a socket has no such gap. Two processes cannot hold the same
 * addr:port (that needs SO_REUSEPORT, which Node does not set), so the kernel
 * arbitrates atomically, and it unbinds on process death however abrupt — no
 * pids, no staleness, no reclaim race. Ports are machine-wide by nature, which
 * is the property the lock directory was straining to fake.
 */
export const reservePort = (port, host = '127.0.0.1') =>
  new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', () => resolve(null))
    server.listen({ port, host, exclusive: true }, () => {
      // Bound for as long as this process lives, but never a reason to keep it
      // alive: the runner must exit as soon as Playwright does.
      server.unref()
      resolve(server)
    })
  })

/** Resolve once a listener has actually bound, so a foreign holder reads as busy. */
export const isPortFree = async (port, host = '127.0.0.1') => {
  const server = await reservePort(port, host)
  if (server === null) return false
  await new Promise((resolve) => server.close(resolve))
  return true
}

/**
 * The descriptor behind the reservation, so the child a caller spawns keeps the
 * slot held even if the caller itself is killed outright.
 *
 * spawn() dups this into the child, which holds the port bound without ever
 * reading from it. A SIGKILLed runner therefore no longer frees a slot out from
 * under the `playwright test` it started.
 *
 * It reaches exactly that one edge, and it is worth being precise about why:
 * Node clears the descriptor on the next exec, so it does not carry on into
 * Playwright's own children, and Playwright starts each web server in its own
 * process group, so there is no group left to signal either. Kill Playwright and
 * its servers outlive the reservation no matter what is done here — which is why
 * nothing about database safety is allowed to rest on this. That comes from
 * naming the database after the run (run-db.mjs); this is only what keeps a
 * killed runner from handing its ports to someone else mid-flight.
 *
 * Null off POSIX, where the caller gets the parent-only lifetime back.
 */
export const reservationFd = (server) => {
  const fd = server._handle?.fd
  return typeof fd === 'number' && fd >= 0 ? fd : null
}

/**
 * Reserve the first slot nothing else holds.
 *
 * Two checks, and both are needed. The reservation socket is what keeps our own
 * concurrent runs apart — it is held for the whole run, so a slot cannot be
 * handed out twice. The probe of the frontend/backend ports is what keeps us off
 * a port some unrelated server already has, which a reservation cannot see: the
 * incident that prompted this was a six-hour-old `vite preview` from another
 * project sitting on the frontend port.
 *
 * The probe is still a check-then-act, so a foreign process could take a probed
 * port in the microseconds before our servers start. That degrades to Vite or
 * uvicorn failing to bind and Playwright reporting it — loud and immediate. The
 * silent modes are the ones that matter, and both are gone: the reservation
 * cannot be double-granted, and the database is named per run rather than per
 * slot, so even a slot handed on early cannot make two runs share one file.
 *
 * @returns {Promise<{slot: number, frontendPort: number, backendPort: number,
 *   reservationPort: number, reservationFd: number | null,
 *   release: () => Promise<void>}>}
 */
export const acquireRunSlot = async (options = {}) => {
  const {
    slotCount = DEFAULT_SLOT_COUNT,
    frontendPortBase = DEFAULT_FRONTEND_PORT_BASE,
    backendPortBase = DEFAULT_BACKEND_PORT_BASE,
    reservationPortBase = DEFAULT_RESERVATION_PORT_BASE,
    portIsFree = isPortFree,
  } = options

  for (let slot = 0; slot < slotCount; slot += 1) {
    const reservationPort = reservationPortBase + slot
    const reservation = await reservePort(reservationPort)
    if (reservation === null) continue

    const close = () =>
      new Promise((resolve) => {
        if (!reservation.listening) return resolve()
        reservation.close(() => resolve())
      })

    const frontendPort = frontendPortBase + slot
    const backendPort = backendPortBase + slot
    if (!(await portIsFree(frontendPort)) || !(await portIsFree(backendPort))) {
      // Release it: a transient holder of one port must not retire the slot for
      // every later run on this machine.
      await close()
      continue
    }

    return {
      slot,
      frontendPort,
      backendPort,
      reservationPort,
      reservationFd: reservationFd(reservation),
      release: close,
    }
  }

  throw new Error(
    `No free e2e run slot: all ${slotCount} of ${frontendPortBase}-${frontendPortBase + slotCount - 1} / ` +
      `${backendPortBase}-${backendPortBase + slotCount - 1} are reserved or in use.\n` +
      `Reservations are listening sockets on ${reservationPortBase}-${reservationPortBase + slotCount - 1}; ` +
      `\`lsof -iTCP:${reservationPortBase}-${reservationPortBase + slotCount - 1} -sTCP:LISTEN\` names the holders.\n` +
      `To pick ports yourself instead, set E2E_FRONTEND_PORT, E2E_BACKEND_PORT and E2E_API_URL.`,
  )
}
