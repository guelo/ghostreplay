# Playwright E2E

This directory contains end-to-end tests powered by Playwright.

## Commands

```bash
npm run test:e2e
npm run test:e2e:headed
npm run test:e2e:ui
npm run test:e2e:screens   # screenshot gallery only
```

Every one of these goes through `scripts/e2e/run.mjs`, which reserves a run slot
before handing off to `playwright test`. Extra arguments pass straight through:
`npm run test:e2e -- e2e/play-2col-graph.spec.ts`.

## Concurrent Runs

This repo is worked on by several agents at once, so runs must be able to
overlap. A slot is a reserved (frontend port, backend port) pair; the database
belongs to the run rather than to the slot, for the reason below. The runner
prints what it took:

```
[e2e] slot 1: frontend 4301, backend 8301, db backend/.tmp/e2e-slot-1-9f3c2a1b7d40.sqlite3
[e2e] report: npx playwright show-report playwright-report/slot-1
```

Artifacts are per-slot too (`test-results/slot-N`, `playwright-report/slot-N`),
because Playwright empties its output directory at startup and would otherwise
delete a concurrent run's traces.

The slot is held by a listening socket on `8400+N`, which the kernel releases
when its holders die, however they die. The runner passes that socket to
Playwright as well, so killing the runner does not free ports that the suite it
started is about to bind. `lsof -iTCP:8400-8423 -sTCP:LISTEN` names the current
holders.

The reservation reaches no further than that. A descriptor does not survive the
next `exec`, and Playwright starts each web server in its own process group, so
a Playwright that dies badly leaves servers running with nothing holding their
slot. **Nothing about database safety depends on the reservation.** The database
is named for the run — `e2e-slot-<slot>-<run>.sqlite3`, where `<run>` is random
rather than the pid, which is unique only among live processes — so two runs
cannot name the same file no matter which processes outlive which. What is left
is a port race in the window before the servers bind, and losing a port is loud:
Vite or uvicorn fails immediately and Playwright reports it.

Leftovers are swept by the next run, per slot, and only when two things agree:
nothing holds that slot — reservation and both service ports free — and the file
has not been written for ten minutes. "Is the pid in the name still alive?" would
be wrong exactly where the reservation works, on a killed runner whose Playwright
is still running against that database. The idle slot is not enough on its own
either: a run that is still seeding has not bound anything yet, so it reads idle
for a few seconds, and it is the ten minutes of silence that tells the two
apart.

**Measured envelope:** two pre-push suites at once pass cleanly on a quiet
machine (29/29 each, ~47s versus ~27s solo). What decides this is CPU, not
isolation, so read it as a property of the machine rather than of the runner: the
same pair failed 5-and-1 while another agent's suite had the box at load 47, and
went green again at load 19. Three at once saturate a laptop outright — three
Chromium+Stockfish-WASM browsers against three Maia backends — including inside
timeouts no config setting reaches (`stockfish-worker.smoke.spec.ts` waits 20s
for its worker from inside `page.evaluate`).

Check `uptime` before blaming a change for that kind of failure. It is loud and
obvious — the suite takes 2m instead of 27s — and it is not the silent kind this
isolation exists to prevent, where a run shares a database with a concurrent
`--reset` and watches rows vanish mid-test (g-e2e-port-collide).

To choose the endpoints yourself instead — debugging, or working around a port
some other project holds — set any of `E2E_FRONTEND_PORT`, `E2E_BACKEND_PORT`,
`E2E_BASE_URL`, `E2E_API_URL`, `E2E_DATABASE_URL`. Any one of them switches slot
allocation off and leaves all five to you, because they are interdependent:
filling in only the ones you left unset is how the backend ends up on one port
and the login fixture on another.

`E2E_OUTPUT_DIR` and `E2E_REPORT_DIR` are separate. They only say where
artifacts land, so setting them keeps per-run port and database isolation — but
the directories you name are then yours to keep unique, since Playwright empties
its output directory at startup.

All of these resolve in exactly one place, `e2e/env.ts`; never re-derive a
default at a call site.

## Screenshot Gallery (`e2e/screenshots/`)

`npm run test:e2e:screens` captures a fixed first-pass inventory of UI states
(loading / empty / populated / error, plus the seeded `/play` review-warning
toast) across the app's real breakpoints. It is a **review artifact, not a
pixel-diff suite** — there are no `toHaveScreenshot()` assertions this pass.

- Output PNGs and a contact-sheet `output/index.html` are written to
  `e2e/screenshots/output/` (gitignored). Open `index.html` to review.
- Screenshots are also attached to the Playwright HTML report.
- The suite runs serial (shared output dir). Determinism comes from a frozen
  clock, the app's reduced-motion branch plus disabled animations, fixed
  client-side random sampling, software Chromium compositing, seeded accounts,
  and route mocks for loading/error/gameplay states (see `helpers.ts`). Each PNG
  is written only after three consecutive screenshot samples are byte-identical.
  The software raster lane keeps rounded-corner and shadow antialiasing stable
  between browser processes while screenshots still use the application's
  production CSS without capture-only shadow, radius, or SVG rendering
  overrides, so decorative cascade rules remain part of the golden.
- History and game-analysis captures keep Engine lines on. A worker scoped to
  the gallery supplies fixed depth-21 output, so the PNGs deterministically
  cover the depth badge, progress/shimmer, populated PV row, and placeholder
  rows without depending on local Stockfish search timing. The live-game
  analysis worker is not mocked.
- Live-game scenarios wait for their visible move-analysis spinners to clear
  before capture so graph values and classifications are final rather than an
  arbitrary intermediate engine state. Timed review-warning captures advance
  their paused clock in small steps during that wait, bounded below the notice's
  dismissal timer, so late cache debounce work cannot be stranded.
- Drill endings come from a REAL drill on the B20 Sicilian Defense root —
  `/api/drills/start`, the steered opponent reply and `/route-check` are all
  unmocked. That root (1.e4 c5) is reached by the OPPONENT's move, which is what
  makes "Opening root reached" hold still long enough to shoot at six viewports;
  the two stops are the Strict-tier accuracy grade on 2.a3 and the off-route
  answer to 1.a3. These tests run last in the file: a drill writes opening
  evidence for the shared seeded account, and that moves the opening-lineage
  scores every later /play capture renders.
- Analysis-board captures wait for piece transforms to reconcile and place the
  selected move at a deterministic scroll offset after responsive layout swaps.

## Seeded Accounts

The backend seed script (`backend/scripts/seed_e2e_data.py`) creates deterministic users:

- `e2e_due_user` / `e2e-pass-123` (has due blunder fixtures)
- `e2e_stable_user` / `e2e-pass-123` (has non-due blunder fixtures)
- `e2e_empty_user` / `e2e-pass-123` (no blunders)

Credentials can be overridden via environment variables:

- `E2E_DUE_USERNAME`, `E2E_DUE_PASSWORD`
- `E2E_STABLE_USERNAME`, `E2E_STABLE_PASSWORD`
- `E2E_EMPTY_USERNAME`, `E2E_EMPTY_PASSWORD`

## Backend Bootstrapping

`playwright.config.ts` starts:

1. `scripts/e2e/start_backend.sh`:
   - activates `backend/.venv`
   - resets and seeds the E2E database named by `DATABASE_URL`
   - runs FastAPI on `127.0.0.1:$BACKEND_PORT`
2. Vite dev server with `VITE_API_URL` pointed at that backend, started with
   `--strictPort` so a taken port fails immediately instead of quietly moving to
   the next one and leaving Playwright waiting on an empty `baseURL`.

Both get their port and database from the slot the runner reserved; see
`e2e/env.ts` and Concurrent Runs above.
