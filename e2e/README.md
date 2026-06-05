# Playwright E2E

This directory contains end-to-end tests powered by Playwright.

## Commands

```bash
npm run test:e2e
npm run test:e2e:headed
npm run test:e2e:ui
npm run test:e2e:screens   # screenshot gallery only
```

## Screenshot Gallery (`e2e/screenshots/`)

`npm run test:e2e:screens` captures a fixed first-pass inventory of UI states
(loading / empty / populated / error, plus the seeded `/play` review-warning
toast) across the app's real breakpoints. It is a **review artifact, not a
pixel-diff suite** — there are no `toHaveScreenshot()` assertions this pass.

- Output PNGs and a contact-sheet `output/index.html` are written to
  `e2e/screenshots/output/` (gitignored). Open `index.html` to review.
- Screenshots are also attached to the Playwright HTML report.
- The suite runs serial (shared output dir). Determinism comes from a frozen
  clock, disabled animations, seeded accounts, and route mocks for
  loading/error states (see `helpers.ts`).
- Harder live-game overlays (promotion picker, review-fail modal, game-end
  banner) and pixel-diff baselines are tracked as follow-up beads
  (`g-yr3a`, `g-r03h`, `g-zwpe`).

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
   - resets and seeds the E2E database
   - runs FastAPI on `127.0.0.1:${E2E_BACKEND_PORT:-8010}`
2. Vite dev server with `VITE_API_URL` pointed at that backend.
